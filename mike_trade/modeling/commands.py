"""Operator CLI for the offline model workflow: data -> train -> evaluate -> backtest -> approve.

Everything here runs offline, in the operator's process, never inside the trading engine.  Heavy
modules (the native tree libraries, DuckDB readers, the backtest runner) are imported only by the
command that needs them, so listing or inspecting models stays instant.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from typing import Any

EXIT_REFUSED = 3
EXIT_UNHEALTHY = 5
_ROOT_DEFAULT = os.environ.get("MIKE_TRADE_MODEL_DIR", "research/models")


def _root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=_ROOT_DEFAULT, help="model repository root")


def register(subparsers: Any) -> None:
    prepare = subparsers.add_parser(
        "model-prepare", help="pin and verify the training data; write an immutable lock"
    )
    prepare.add_argument("--config", required=True, help="versioned model YAML configuration")
    prepare.set_defaults(func=run_prepare)

    train = subparsers.add_parser("model-train", help="train and persist one configured model")
    train.add_argument("--config", required=True, help="versioned model YAML/JSON configuration")
    train.add_argument("--lock", help="model-prepare lock whose exact snapshots must be used")
    train.set_defaults(func=run_train)

    walk = subparsers.add_parser(
        "model-walk-forward", help="purged walk-forward validation (no model is stored)"
    )
    walk.add_argument("--config", required=True)
    walk.add_argument("--lock", help="model-prepare lock whose exact snapshots must be used")
    walk.set_defaults(func=run_walk_forward)

    evaluate = subparsers.add_parser(
        "model-evaluate", help="re-evaluate a pinned model on its configured evaluation range"
    )
    evaluate.add_argument("--config", required=True)
    evaluate.set_defaults(func=run_evaluate)

    inspect = subparsers.add_parser("model-inspect", help="inspect manifest and evaluation")
    inspect.add_argument("--model-id", required=True)
    inspect.add_argument("--version", required=True)
    _root(inspect)
    inspect.set_defaults(func=run_inspect)

    listing = subparsers.add_parser("model-list", help="list immutable model versions")
    _root(listing)
    listing.set_defaults(func=run_list)

    compare = subparsers.add_parser("model-compare", help="compare stored predictive evaluations")
    compare.add_argument("--models", required=True, help="comma-separated model_id@version")
    _root(compare)
    compare.set_defaults(func=run_compare)

    report = subparsers.add_parser(
        "model-report", help="write the JSON/Markdown/CSV report of one saved model"
    )
    report.add_argument("--model-id", required=True)
    report.add_argument("--version", required=True)
    report.add_argument("--output-dir", help="default: <root>/reports/<model_id>/<version>")
    report.add_argument("--print", dest="print_markdown", action="store_true")
    _root(report)
    report.set_defaults(func=run_report)

    backtest = subparsers.add_parser(
        "model-backtest", help="run a model-driven backtest campaign on the research runner"
    )
    backtest.add_argument("--campaign", required=True, help="versioned campaign YAML")
    backtest.add_argument(
        "--workers", default=None, help="worker processes: integer or auto (default: campaign)"
    )
    backtest.add_argument("--dry-run", action="store_true", help="validate and print the plan")
    backtest.add_argument(
        "--expect",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="required effective setting (e.g. slippage_bps=2, planned_jobs=792); a mismatch "
        "fails the launch before any job starts. Repeatable.",
    )
    backtest.add_argument("--skip-missing", action="store_true", help="skip symbols lacking data")
    backtest.set_defaults(func=run_backtest)

    compare_runs = subparsers.add_parser(
        "model-backtest-compare", help="compare model-backtest reports"
    )
    compare_runs.add_argument("--reports", required=True, help="comma-separated report.json paths")
    compare_runs.add_argument("--output", help="write the comparison JSON here")
    compare_runs.set_defaults(func=run_backtest_compare)

    approve = subparsers.add_parser(
        "model-approve", help="record an immutable operator approval for one exact model"
    )
    approve.add_argument("--model-id", required=True)
    approve.add_argument("--version", required=True)
    approve.add_argument("--stage", required=True, choices=("shadow", "paper", "live"))
    approve.add_argument("--approved-by", default=None, help="default: the current OS user")
    approve.add_argument("--reason", required=True)
    approve.add_argument(
        "--evidence", action="append", default=[], help="model-backtest report.json (paper/live)"
    )
    approve.add_argument("--confirm-live", default="", help="YES, required for the live stage")
    _root(approve)
    approve.set_defaults(func=run_approve)

    pin = subparsers.add_parser(
        "model-pin", help="print the exact strategy configuration that pins an approved model"
    )
    pin.add_argument("--model-id", required=True)
    pin.add_argument("--version", required=True)
    pin.add_argument("--name", help="strategies: entry name (default: <model_id>)")
    pin.add_argument("--stage", default="shadow", choices=("shadow", "paper", "live"))
    pin.add_argument("--output", help="write the YAML snippet to this file")
    _root(pin)
    pin.set_defaults(func=run_pin)

    check = subparsers.add_parser(
        "model-check", help="offline pre-flight of every model pin in a trading configuration"
    )
    check.add_argument("--config", default=None, help="default: $MIKE_TRADE_CONFIG")
    check.add_argument("--mode", choices=("shadow", "paper", "live"), default=None)
    check.set_defaults(func=run_check)

    health = subparsers.add_parser(
        "model-health", help="model status published by the running engine"
    )
    health.add_argument(
        "--db", default=os.environ.get("MIKE_TRADE_DB_PATH", "state/dev/shadow.sqlite3")
    )
    health.set_defaults(func=run_health)


def _print(value: object) -> None:
    print(json.dumps(value, sort_keys=True, indent=2, default=str))


# ------------------------------------------------------------------ data and training


def run_prepare(args: argparse.Namespace) -> int:
    from mike_trade.modeling.config import load_experiment_config
    from mike_trade.modeling.operations import prepare_training_lock

    config = load_experiment_config(args.config)
    path, payload = prepare_training_lock(config)
    _print(
        {
            "lock": str(path),
            **{k: payload[k] for k in ("feed", "counts", "resources")},
            "symbols": len(payload["dataset_sources"]),
        }
    )
    return 0


def _lock_arguments(args: argparse.Namespace, config: Any) -> dict[str, Any]:
    if not getattr(args, "lock", None):
        return {}
    from mike_trade.modeling.operations import read_training_lock

    lock = read_training_lock(args.lock, config)
    return {
        "dataset_ids": {s["symbol"]: s["dataset_id"] for s in lock["dataset_sources"]},
        "expected_sha256": {s["symbol"]: s["sha256"] for s in lock["dataset_sources"]},
    }


def run_train(args: argparse.Namespace) -> int:
    from mike_trade.modeling.config import load_experiment_config
    from mike_trade.modeling.training import ModelTrainingService

    config = load_experiment_config(args.config)
    outcome = ModelTrainingService(config).train(**_lock_arguments(args, config))
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
            "next": "model-report, then model-backtest; training does not make a model tradeable",
        }
    )
    return 0


def run_walk_forward(args: argparse.Namespace) -> int:
    from mike_trade.modeling.config import load_experiment_config
    from mike_trade.modeling.walk_forward import run_walk_forward as execute

    config = load_experiment_config(args.config)
    lock = _lock_arguments(args, config)
    path, payload = execute(config, dataset_ids=lock.get("dataset_ids"))
    _print({"summary": str(path), "folds": len(payload["folds"]), "metrics": payload["summary"]})
    return 0


def run_evaluate(args: argparse.Namespace) -> int:
    from mike_trade.modeling.config import load_experiment_config
    from mike_trade.modeling.training import evaluate_saved_model

    evaluation = evaluate_saved_model(load_experiment_config(args.config))
    _print(json.loads(evaluation.to_json_bytes()))
    return 0


# ------------------------------------------------------------------ inspection


def run_inspect(args: argparse.Namespace) -> int:
    from mike_trade.modeling.approvals import ApprovalRepository
    from mike_trade.modeling.artifacts import FileModelRepository

    repository = FileModelRepository(args.root)
    manifest, _ = repository.get(args.model_id, args.version)
    evaluation = (
        json.loads(repository.get_evaluation(args.model_id, args.version))
        if manifest.evaluation_sha256 is not None
        else None
    )
    _print(
        {
            "manifest": manifest.to_mapping(),
            "evaluation": evaluation,
            "approval": ApprovalRepository(args.root).highest_valid_stage(manifest),
        }
    )
    return 0


def run_list(args: argparse.Namespace) -> int:
    from mike_trade.modeling.approvals import ApprovalRepository
    from mike_trade.modeling.artifacts import FileModelRepository

    approvals = ApprovalRepository(args.root)
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
                "approval": approvals.highest_valid_stage(item),
            }
            for item in manifests
        ]
    )
    return 0


def run_compare(args: argparse.Namespace) -> int:
    from mike_trade.modeling.artifacts import FileModelRepository

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


def run_report(args: argparse.Namespace) -> int:
    from mike_trade.modeling.model_reports import write_model_report

    paths = write_model_report(args.root, args.model_id, args.version, args.output_dir)
    if args.print_markdown:
        print(paths["markdown"].read_text(encoding="utf-8"))
    _print({name: str(path) for name, path in paths.items()})
    return 0


# ------------------------------------------------------------------ backtest and comparison


def run_backtest(args: argparse.Namespace) -> int:
    from mike_trade.modeling.campaign import (
        CampaignError,
        load_campaign,
        plan_campaign,
        run_campaign,
    )

    from mike_trade.modeling.preflight import PreflightError, enforce, format_preflight, preflight

    try:
        expectations = dict(_parse_expectation(item) for item in args.expect)
        spec = load_campaign(args.campaign)
        plan = plan_campaign(spec, skip_missing=args.skip_missing)
        effective = preflight(plan, workers=args.workers)
        print(format_preflight(effective), flush=True)
        enforce(effective, {**spec.expect, **expectations})
    except (CampaignError, PreflightError) as exc:
        print(f"Campaign refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    if args.dry_run:
        _print({"dry_run": True, **plan.summary()})
        return 0
    try:
        outcome = run_campaign(plan, workers=args.workers, expect=expectations)
    except CampaignError as exc:
        print(f"Campaign refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    _print({"run_directory": str(spec.run_directory), **outcome})
    return 0 if outcome["batch"]["failed_count"] == 0 else EXIT_UNHEALTHY


def _parse_expectation(item: str) -> tuple[str, str]:
    key, separator, value = item.partition("=")
    if not separator or not key.strip():
        raise CampaignError(f"--expect needs KEY=VALUE, got {item!r}")
    return key.strip(), value.strip()


def run_backtest_compare(args: argparse.Namespace) -> int:
    from mike_trade.modeling.operations import compare_reports

    result = compare_reports([item.strip() for item in args.reports.split(",") if item.strip()])
    if args.output:
        Path(args.output).write_text(
            json.dumps(result, sort_keys=True, indent=2, default=str) + "\n", encoding="utf-8"
        )
    _print(result)
    return 0


# ------------------------------------------------------------------ promotion and runtime


def run_approve(args: argparse.Namespace) -> int:
    from mike_trade.modeling.approvals import ApprovalError, approve_model

    if args.stage == "live" and (args.confirm_live or os.environ.get("CONFIRM_LIVE")) != "YES":
        print(
            "Refused: a live approval requires --confirm-live YES (or CONFIRM_LIVE=YES).",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    try:
        path, approval = approve_model(
            args.root,
            args.model_id,
            args.version,
            args.stage,
            approved_by=args.approved_by or getpass.getuser(),
            reason=args.reason,
            evidence=args.evidence,
        )
    except ApprovalError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    _print(
        {
            "approval": str(path),
            **approval.to_mapping(),
            "note": "An approval permits the mode to load this exact artifact; it starts nothing.",
        }
    )
    return 0


def run_pin(args: argparse.Namespace) -> int:
    import yaml  # type: ignore[import-untyped]

    from mike_trade.modeling.approvals import STAGES, ApprovalRepository, pin_parameters
    from mike_trade.modeling.artifacts import FileModelRepository

    manifest, _ = FileModelRepository(args.root).get(args.model_id, args.version)
    highest = ApprovalRepository(args.root).highest_valid_stage(manifest)
    if highest is None or STAGES.index(highest) < STAGES.index(args.stage):
        print(
            f"Refused: {args.model_id}@{args.version} is not approved for '{args.stage}' "
            f"(highest valid: {highest or 'none'}). Run model-approve first.",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    role = "sell" if manifest.feature_schema.feature_id == "intraday_position" else "buy"
    name = args.name or manifest.model_id
    snippet = yaml.safe_dump(
        {
            "strategies": {
                name: {
                    "plugin": f"{manifest.algorithm}_probability_{role}",
                    "side": role,
                    "parameters": pin_parameters(manifest),
                }
            }
        },
        sort_keys=False,
    )
    if args.output:
        Path(args.output).write_text(snippet, encoding="utf-8")
    print(snippet)
    print(
        f"# Add thresholds under parameters, list '{name}' under the symbol's "
        f"{role}_strategies, and set market_data.feed: {manifest.feed}.\n"
        f"# service.model_root must point at {args.root}."
    )
    return 0


def run_check(args: argparse.Namespace) -> int:
    from mike_trade.modeling.operations import check_configuration

    report = check_configuration(args.config, mode=args.mode)
    _print(report)
    return 0 if report["ok"] else EXIT_UNHEALTHY


def run_health(args: argparse.Namespace) -> int:
    from mike_trade.modeling.operations import read_model_health

    health = read_model_health(args.db)
    _print(health)
    return 0 if health.get("published") else EXIT_UNHEALTHY
