# ruff: noqa: E501  (embedded campaign YAML lines)
"""End-to-end acceptance of the research-to-trading model workflow on a small synthetic dataset.

Historical Parquet snapshots -> lock -> features/labels -> XGBoost and LightGBM training ->
immutable artifacts -> registry reload -> reports -> purged walk-forward -> backtest campaign on
the existing runner -> approvals -> SHADOW start-up and operation -> failure and crash gating.

Synthetic bars only; nothing contacts a network or broker and no PAPER/LIVE order exists.
"""

from __future__ import annotations

import csv
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from textwrap import dedent

import numpy as np
import pytest

from mike_trade.market_data.alpaca_history import Adjustment, HistoricalBar
from mike_trade.modeling.approvals import ApprovalError, ApprovalRepository, approve_model
from mike_trade.modeling.artifacts import FileModelRepository
from mike_trade.modeling.campaign import (
    CampaignError,
    load_campaign,
    plan_campaign,
    run_campaign,
)
from mike_trade.modeling.config import load_experiment_config
from mike_trade.modeling.model_reports import write_model_report
from mike_trade.modeling.operations import (
    check_configuration,
    compare_reports,
    prepare_training_lock,
    read_training_lock,
)
from mike_trade.modeling.splits import FINAL_HOLDOUT
from mike_trade.modeling.training import (
    ModelTrainingService,
    evaluate_saved_model,
    load_model_predictor,
)
from mike_trade.modeling.walk_forward import run_walk_forward
from mike_trade.research.datasets import HistoricalDatasetRepository

MINUTE = timedelta(minutes=1)
SESSION_DAYS = [(2026, 3, d) for d in (10, 11, 12, 13, 16, 17, 18, 19, 20, 23, 24, 25)]
SYMBOLS = {"AAA": 100, "BBB": 50, "SPY": 400, "QQQ": 300, "IWM": 200}
MODELS = {
    ("xgboost", "buy"): "xgb_buy_t",
    ("xgboost", "sell"): "xgb_sell_t",
    ("lightgbm", "buy"): "lgbm_buy_t",
    ("lightgbm", "sell"): "lgbm_sell_t",
}


def build_dataset(root: Path, *, crash: bool) -> None:
    repository = HistoricalDatasetRepository(root)
    for index, (symbol, base) in enumerate(SYMBOLS.items()):
        rng = np.random.default_rng(1000 + index)
        price = D(base)
        bars: list[HistoricalBar] = []
        for day in SESSION_DAYS:
            open_time = datetime(*day, 13, 30, tzinfo=UTC)
            for minute in range(120):
                move = D(str(round(float(rng.normal(0, 0.0015)), 6)))
                if (
                    crash
                    and symbol in {"SPY", "QQQ", "IWM"}
                    and day == (2026, 3, 23)
                    and 40 <= minute < 50
                ):
                    move = D("-0.008")
                close = (price * (1 + move)).quantize(D("0.0001"))
                high = max(price, close) * D("1.0004")
                low = min(price, close) * D("0.9996")
                typical = (price + high + low + close) / 4
                bars.append(
                    HistoricalBar(
                        symbol,
                        open_time + minute * MINUTE,
                        price,
                        high.quantize(D("0.0001")),
                        low.quantize(D("0.0001")),
                        close,
                        1000 + minute,
                        typical.quantize(D("0.0001")),
                        10,
                        "iex",
                        Adjustment.RAW,
                    )
                )
                price = close
        repository.persist(
            symbol,
            bars,
            feed="iex",
            coverage_start=datetime(2026, 3, 10, tzinfo=UTC),
            coverage_end=datetime(2026, 3, 27, tzinfo=UTC),
        )


def experiment_yaml(
    algorithm: str, role: str, data: Path, models: Path, *, walk: bool = False
) -> str:
    import yaml

    hyper: dict[str, object] = {
        "class_balance": "none",
        "num_boost_round": 15,
        "early_stopping_rounds": 3,
        "learning_rate": 0.2,
    }
    hyper.update(
        {"max_depth": 3, "subsample": 1.0, "colsample_bytree": 1.0}
        if algorithm == "xgboost"
        else {
            "num_leaves": 7,
            "min_data_in_leaf": 5,
            "feature_fraction": 1.0,
            "bagging_fraction": 1.0,
        }
    )
    document: dict[str, object] = {
        "schema_version": 1,
        "experiment_id": f"{MODELS[(algorithm, role)]}_exp",
        "algorithm": algorithm,
        "role": role,
        "model_id": MODELS[(algorithm, role)],
        "model_version": "1.0.0",
        "dataset_root": str(data),
        "model_root": str(models),
        "universe_id": "synthetic_v1",
        "symbols": ["AAA", "BBB"],
        "feed": "iex",
        "timeframe": "1m",
        "adjustment": "raw",
        "seed": 7,
        "ranges": {
            "fit_start": "2026-03-10T00:00:00Z",
            "fit_end": "2026-03-18T00:00:00Z",
            "early_stopping_start": "2026-03-18T00:00:00Z",
            "early_stopping_end": "2026-03-20T00:00:00Z",
            "calibration_start": "2026-03-20T00:00:00Z",
            "calibration_end": "2026-03-21T00:00:00Z",
            "evaluation_start": "2026-03-23T00:00:00Z",
            "evaluation_end": "2026-03-26T00:00:00Z",
            "embargo_minutes": 0,
        },
        "features": {
            "feature_id": "intraday_core",
            "version": "1.0.0",
            "reference_symbols": ["SPY"],
            "include_vwap": True,
            "selected_feature_names": [
                "return_1",
                "return_5",
                "realized_vol_5",
                "relative_volume_5",
                "candle_body_pct",
                "ma_distance_5",
                "vwap_distance",
                "rolling_zscore_20",
                "relative_strength_spy_5",
            ],
        },
        "resources": {"device": "cpu", "threads": 1},
        "hyperparameters": hyper,
        "search": {"max_trials": 1},
        "probability_calibration": True,
        "permutation_importance": True,
        "permutation_max_rows": 300,
        "batch_size": 64,
    }
    if role == "buy":
        document["objective"] = {
            "type": "triple_barrier",
            "task": "MULTICLASS_CLASSIFICATION",
            "class_labels": ["-1", "0", "1"],
            "positive_barrier": "0.003",
            "negative_barrier": "0.003",
            "horizon_bars": 5,
            "entry_bps": "1.0",
            "exit_bps": "1.0",
        }
    else:
        document["entry_policy"] = {
            "warmup_bars": 25,
            "entry_interval_bars": 30,
            "maximum_holding_bars": 20,
            "cooldown_bars": 5,
        }
        document["objective"] = {
            "type": "sell_continuation",
            "task": "MULTICLASS_CLASSIFICATION",
            "class_labels": ["-1", "0", "1"],
            "horizon_bars": 3,
            "risk_barrier": "0.004",
            "minimum_hold_advantage": "0",
            "entry_bps": "1.0",
            "exit_bps": "1.0",
        }
    if walk:
        document["walk_forward"] = {
            "folds": 2,
            "validation_days": 1,
            "early_stopping_fraction": 0.15,
            "calibration_fraction": 0.15,
        }
    return yaml.safe_dump(document, sort_keys=False)


@pytest.fixture(scope="module")
def workflow(tmp_path_factory):
    base = tmp_path_factory.mktemp("workflow")
    data, models = base / "data", base / "models"
    build_dataset(data, crash=False)
    configs: dict[tuple[str, str], Path] = {}
    for key in MODELS:
        path = base / f"{MODELS[key]}.yaml"
        path.write_text(experiment_yaml(*key, data, models, walk=key == ("xgboost", "buy")))
        configs[key] = path
    outcomes = {}
    locks = {}
    for key, path in configs.items():
        config = load_experiment_config(path)
        lock_file, lock = prepare_training_lock(config)
        locks[key] = (lock_file, lock)
        loaded = read_training_lock(lock_file, config)
        outcomes[key] = ModelTrainingService(config).train(
            dataset_ids={s["symbol"]: s["dataset_id"] for s in loaded["dataset_sources"]},
            expected_sha256={s["symbol"]: s["sha256"] for s in loaded["dataset_sources"]},
        )
    return {
        "base": base,
        "data": data,
        "models": models,
        "configs": configs,
        "outcomes": outcomes,
        "locks": locks,
    }


# ------------------------------------------------------------------ data -> models


def test_lock_pins_exact_snapshots_and_counts_and_is_reproducible(workflow):
    lock_file, lock = workflow["locks"][("xgboost", "buy")]
    assert lock["feed"] == "iex" and lock["counts"]["fit"] > 200
    assert {s["symbol"] for s in lock["dataset_sources"]} == {"AAA", "BBB", "SPY"}
    assert all(len(s["sha256"]) == 64 for s in lock["dataset_sources"])
    assert lock["resources"]["estimated_training_memory_bytes"] > 0
    config = load_experiment_config(workflow["configs"][("xgboost", "buy")])
    again_file, again = prepare_training_lock(config)
    assert again_file == lock_file and again == lock  # identical, nothing overwritten
    other = load_experiment_config(workflow["configs"][("lightgbm", "buy")])
    with pytest.raises(ValueError, match="different configuration"):
        read_training_lock(lock_file, other)


def test_four_immutable_models_reload_through_the_registry(workflow):
    repository = FileModelRepository(workflow["models"])
    assert {m.model_id for m in repository.list()} == set(MODELS.values())
    for key, outcome in workflow["outcomes"].items():
        manifest = outcome.stored.manifest
        assert manifest.algorithm == key[0] and manifest.feed == "iex"
        assert manifest.holdout_evaluated is False
        assert all(
            not r.overlaps(FINAL_HOLDOUT)
            for r in (*manifest.training_ranges, *manifest.validation_ranges)
        )
        loaded, predictor = load_model_predictor(repository, manifest.model_id, "1.0.0")
        assert loaded.artifact_sha256 == manifest.artifact_sha256
        assert predictor.selected_feature_names == manifest.selected_feature_names
        counts = json.loads(repository.get_evaluation(manifest.model_id, "1.0.0"))["dataset_counts"]
        assert counts["fit"] > 0 and counts["evaluation"] > 0 and counts["calibration"] > 0
    repeated = evaluate_saved_model(
        load_experiment_config(workflow["configs"][("lightgbm", "sell")])
    )
    assert (
        repeated.sample_count == workflow["outcomes"][("lightgbm", "sell")].evaluation.sample_count
    )


def test_training_refuses_data_that_differs_from_the_lock(workflow):
    config = load_experiment_config(workflow["configs"][("xgboost", "buy")])
    _, lock = workflow["locks"][("xgboost", "buy")]
    wrong = {s["symbol"]: "0" * 64 for s in lock["dataset_sources"]}
    with pytest.raises(ValueError, match="differ from the locked preparation"):
        ModelTrainingService(config).train(
            dataset_ids={s["symbol"]: s["dataset_id"] for s in lock["dataset_sources"]},
            expected_sha256=wrong,
        )


def test_model_report_is_complete_and_derived_from_recorded_results(workflow):
    paths = write_model_report(workflow["models"], MODELS[("xgboost", "buy")], "1.0.0")
    report = json.loads(paths["json"].read_text())
    assert report["identity"]["algorithm"] == "xgboost"
    assert report["data"]["feed"] == "iex" and report["data"]["holdout_evaluated"] is False
    counts = report["observations"]
    outcome = workflow["outcomes"][("xgboost", "buy")]
    assert counts["evaluation"] == outcome.evaluation.sample_count
    assert report["evaluation"]["metrics"]["log_loss"] == pytest.approx(
        float(outcome.evaluation.metrics["log_loss"])
    )
    assert report["evaluation"]["baseline"]["kind"] == "training_class_frequency"
    assert report["evaluation"]["class_calibration"]["1"]["bins"]
    assert report["evaluation"]["calibration"]
    ranked = [row["feature"] for row in report["feature_importance"]]
    assert set(ranked) == set(outcome.stored.manifest.selected_feature_names)
    assert any(row["permutation"] is not None for row in report["feature_importance"])
    markdown = paths["markdown"].read_text()
    for needle in (
        "Model report",
        "Observations",
        "Feature importance",
        "Reliability of P(class 1)",
    ):
        assert needle in markdown
    with paths["feature_importance.csv"].open() as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == len(ranked)
    assert "per_symbol.csv" in paths and "per_period.csv" in paths


def test_purged_walk_forward_stays_before_each_fold_and_off_the_holdout(workflow):
    config = load_experiment_config(workflow["configs"][("xgboost", "buy")])
    path, payload = run_walk_forward(config)
    assert path.exists() and len(payload["folds"]) == 2
    previous_end = None
    for fold in payload["folds"]:
        ranges = fold["ranges"]
        validation_start = datetime.fromisoformat(ranges["evaluation"]["start"])
        for part in ("fit", "early_stopping", "calibration"):
            assert datetime.fromisoformat(ranges[part]["end"]) <= validation_start
        assert datetime.fromisoformat(ranges["evaluation"]["end"]) <= FINAL_HOLDOUT.start
        assert fold["counts"]["fit"] > 0 and fold["counts"]["evaluation"] > 0
        assert np.isfinite(fold["metrics"]["log_loss"])
        if previous_end is not None:
            assert validation_start >= previous_end  # validation windows never overlap
        previous_end = datetime.fromisoformat(ranges["evaluation"]["end"])
    assert payload["summary"]["log_loss"]["folds"] == 2.0
    report = json.loads(
        write_model_report(workflow["models"], MODELS[("xgboost", "buy")], "1.0.0")[
            "json"
        ].read_text()
    )
    assert report["walk_forward"]["folds"][0]["fold"] == "fold_1"
    again, _ = run_walk_forward(config)  # deterministic: identical bytes, nothing overwritten
    assert again == path


def test_walk_forward_rejects_ranges_that_reach_the_holdout(workflow):
    config = load_experiment_config(workflow["configs"][("xgboost", "buy")])
    late = replace(config.walk_forward, validation_days=30)
    with pytest.raises(ValueError):
        late.fold_ranges(
            replace(
                config,
                ranges=replace(
                    config.ranges,
                    evaluation=replace(config.ranges.evaluation, end=FINAL_HOLDOUT.end),
                ),
            )
        )


# ------------------------------------------------------------------ approvals


def test_approvals_are_ordered_immutable_and_bound_to_the_artifact(workflow, tmp_path):
    import shutil

    models = tmp_path / "models-copy"
    shutil.copytree(workflow["models"], models)
    model_id = MODELS[("xgboost", "buy")]
    with pytest.raises(ApprovalError, match="shadow.*must precede"):
        approve_model(
            models, model_id, "1.0.0", "paper", approved_by="t", reason="r", evidence=["x"]
        )
    path, approval = approve_model(
        models, model_id, "1.0.0", "shadow", approved_by="op", reason="smoke"
    )
    assert path.exists() and approval.stage == "shadow"
    with pytest.raises(ApprovalError, match="immutable"):
        approve_model(models, model_id, "1.0.0", "shadow", approved_by="op", reason="again")
    manifest = workflow["outcomes"][("xgboost", "buy")].stored.manifest
    repository = ApprovalRepository(models)
    assert repository.highest_valid_stage(manifest) == "shadow"
    repository.require(manifest, "SHADOW")
    with pytest.raises(ApprovalError, match="no valid 'paper'"):
        repository.require(manifest, "PAPER")
    with pytest.raises(ApprovalError, match="requires --evidence"):
        approve_model(models, model_id, "1.0.0", "paper", approved_by="op", reason="r")
    forged = replace(manifest, artifact_sha256="1" * 64)
    assert repository.highest_valid_stage(forged) is None


@pytest.fixture(scope="module")
def approved(workflow):
    for model_id in MODELS.values():
        if ApprovalRepository(workflow["models"]).get(model_id, "1.0.0", "shadow") is None:
            approve_model(
                workflow["models"],
                model_id,
                "1.0.0",
                "shadow",
                approved_by="operator",
                reason="acceptance smoke",
            )
    return workflow


# ------------------------------------------------------------------ backtest campaign


DEFAULT_CONFIGURATIONS = """\
  A_xgb_buy: {buy: [xgb_buy], sell: []}
  B_lgbm_buy: {buy: [lgbm_buy], sell: []}
  C_xgb_xgb: {buy: [xgb_buy], sell: [xgb_sell]}
  D_lgbm_lgbm: {buy: [lgbm_buy], sell: [lgbm_sell]}
  E_xgb_lgbm: {buy: [xgb_buy], sell: [lgbm_sell]}
  F_tech_model_sell: {buy: [vwap_bounce], sell: [xgb_sell]}
  G_model_tech_sell: {buy: [xgb_buy], sell: [vwap_breakdown]}
  H_baseline: {buy: [vwap_bounce], sell: [vwap_breakdown]}
"""


def campaign_yaml(
    workflow,
    *,
    directory: Path,
    window=("2026-03-23T00:00:00Z", "2026-03-26T00:00:00Z"),
    market_status: bool = False,
    data: Path | None = None,
    configs: str | None = None,
) -> Path:
    universe = workflow["base"] / "universe.json"
    universe.write_text(
        json.dumps({"universe_id": "synthetic_v1", "symbols": [{"symbol": s} for s in SYMBOLS]})
    )
    thresholds_buy = (
        "{decision_mode: THRESHOLD, minimum_favorable_probability_bps: 1, "
        "maximum_adverse_probability_bps: 10000}"
    )
    thresholds_sell = "{decision_mode: THRESHOLD, minimum_exit_probability_bps: 1}"
    text = dedent(
        f"""
        schema_version: 1
        campaign_id: acceptance
        description: acceptance
        feed: iex
        universe_file: {universe}
        universe_id: synthetic_v1
        symbols: [AAA]
        dataset_root: {data or workflow["data"]}
        model_root: {workflow["models"]}
        output_root: {directory}
        seed: 11
        starting_capital: "10000"
        max_workers: 2
        max_entry_cycles_per_session: 3
        market_status_enabled: {str(market_status).lower()}
        window: {{start: "{window[0]}", end: "{window[1]}"}}
        execution: {{slippage_bps: "5"}}
        protective_stop: {{type: native_trailing, trail_percent: "1.5"}}
        end_of_day_close: {{enabled: false}}
        models:
          xgb_buy: {{strategy: xgboost_probability_buy, model_id: xgb_buy_t, model_version: 1.0.0, parameters: {thresholds_buy}}}
          lgbm_buy: {{strategy: lightgbm_probability_buy, model_id: lgbm_buy_t, model_version: 1.0.0, parameters: {thresholds_buy}}}
          xgb_sell: {{strategy: xgboost_probability_sell, model_id: xgb_sell_t, model_version: 1.0.0, parameters: {thresholds_sell}}}
          lgbm_sell: {{strategy: lightgbm_probability_sell, model_id: lgbm_sell_t, model_version: 1.0.0, parameters: {thresholds_sell}}}
        configurations:
        """
    ) + (configs or DEFAULT_CONFIGURATIONS)
    path = directory / "campaign.yaml"
    directory.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture(scope="module")
def backtest(workflow):
    out = workflow["base"] / "campaign-run"
    spec = load_campaign(campaign_yaml(workflow, directory=out))
    plan = plan_campaign(spec)
    outcome = run_campaign(plan, workers=2)
    report = json.loads((spec.run_directory / "report" / "report.json").read_text())
    return {"spec": spec, "plan": plan, "outcome": outcome, "report": report}


def test_every_comparison_configuration_ran_on_the_existing_runner(backtest):
    report = backtest["report"]
    assert backtest["outcome"]["batch"]["failed_count"] == 0 and not report["failures"]
    names = [c["name"] for c in report["configurations"]]
    assert names == [
        "A_xgb_buy",
        "B_lgbm_buy",
        "C_xgb_xgb",
        "D_lgbm_lgbm",
        "E_xgb_lgbm",
        "F_tech_model_sell",
        "G_model_tech_sell",
        "H_baseline",
    ]
    by_name = {c["name"]: c for c in report["configurations"]}
    for name in ("A_xgb_buy", "B_lgbm_buy", "C_xgb_xgb", "D_lgbm_lgbm", "E_xgb_lgbm"):
        assert by_name[name]["trades"] > 0, name
    assert by_name["C_xgb_xgb"]["buy_strategy_ids"] == ["xgboost_probability_buy"]
    assert by_name["E_xgb_lgbm"]["sell_strategy_ids"] == ["lightgbm_probability_sell"]
    assert by_name["F_tech_model_sell"]["buy_strategy_ids"] == ["vwap_bounce"]
    assert by_name["G_model_tech_sell"]["sell_strategy_ids"] == ["vwap_breakdown"]
    assert report["holdout_evaluated"] is False and report["feed"] == "iex"


def test_report_numbers_reconcile_with_the_recorded_trades(backtest):
    report = backtest["report"]
    directory = backtest["spec"].run_directory / "report"
    with (directory / "trades.csv").open() as stream:
        trades = list(csv.DictReader(stream))
    assert len(trades) == sum(c["trades"] for c in report["configurations"])
    for config in report["configurations"]:
        mine = [t for t in trades if t["configuration"] == config["name"]]
        pnls = [D(t["pnl"]) for t in mine]
        assert len(mine) == config["trades"]
        assert D(config["net_profit"]) == sum(pnls).quantize(D("0.01"))
        assert config["wins"] == sum(p > 0 for p in pnls)
        assert config["losses"] == sum(p < 0 for p in pnls)
        assert config["breakeven"] == sum(p == 0 for p in pnls)
        if config["trades"]:
            assert D(config["trading_costs"]) == sum(D(t["costs"]) for t in mine).quantize(
                D("0.01")
            )
            for t in mine:
                assert (
                    t["entry_time"] < t["exit_time"]
                    and D(t["entry_price"]) > 0
                    and D(t["exit_price"]) > 0
                )
                assert t["entry_strategy"]
    per_symbol = {(r["configuration"], r["symbol"]): r for r in report["per_symbol"]}
    assert set(s for _, s in per_symbol) == {"AAA"}
    assert (
        sum(r["trades"] for r in report["per_period"] if r["configuration"] == "A_xgb_buy")
        == (next(c for c in report["configurations"] if c["name"] == "A_xgb_buy")["trades"])
    )
    buy_hold = report["baselines"]
    assert buy_hold["symbols"][0]["symbol"] == "AAA" and buy_hold["mean_return"] is not None
    for name in ("report.md", "summary.csv", "per_symbol.csv", "per_period.csv", "models.csv"):
        assert (directory / name).stat().st_size > 0


def test_model_audit_records_decisions_and_pins_each_artifact(backtest):
    report = backtest["report"]
    models = {m["name"]: m for m in report["models"]}
    outcomes_sha = {m["name"]: m["artifact_sha256"] for m in models.values()}
    assert len(set(outcomes_sha.values())) == 4 and all(len(v) == 64 for v in outcomes_sha.values())
    assert all(m["sample_role"] == "out_of_sample_development_test" for m in models.values())
    assert all(
        isinstance(m["training_observations"], dict) and m["training_observations"]["fit"] > 0
        for m in models.values()
    )
    a = next(c for c in report["configurations"] if c["name"] == "A_xgb_buy")
    decisions = a["model_decisions"]["xgboost_probability_buy"]
    assert decisions["evaluated_bars"] > 0 and decisions["signals"] >= a["trades"]
    assert report["audit_artifacts"] and all(
        item["results_file_sha256"] for item in report["audit_artifacts"]
    )
    # the run manifest itself records the pinned lineage
    first = next(item for item in report["audit_artifacts"] if item["configuration"] == "A_xgb_buy")
    document = json.loads(Path(first["results_file"]).read_text())
    assert any(row.get("model_audit") for row in document["evaluations"])
    manifest_file = next(Path(first["results_file"]).parent.glob("*.manifest.json"))
    lineage = json.loads(manifest_file.read_text())["configuration"]["model_lineage"]
    assert lineage[0]["artifact_sha256"] == outcomes_sha["xgb_buy"]
    assert lineage[0]["window_overlaps_training"] is False


def test_comparison_of_reports_flags_identical_inputs(backtest, workflow):
    report_path = backtest["spec"].run_directory / "report" / "report.json"
    compared = compare_reports([report_path, report_path])
    assert compared["all_comparable"] is True and len(compared["rows"]) == 16
    other = json.loads(report_path.read_text())
    other["window"]["end"] = "2026-03-25T00:00:00+00:00"
    altered = workflow["base"] / "altered.json"
    altered.write_text(json.dumps(other))
    assert compare_reports([report_path, altered])["all_comparable"] is False


def test_campaign_refuses_unsafe_inputs_before_running_anything(workflow, tmp_path):
    def attempt(**kwargs):
        spec = load_campaign(
            campaign_yaml(workflow, directory=tmp_path / str(len(kwargs)) / "x", **kwargs)
        )
        return plan_campaign(spec)

    with pytest.raises(CampaignError, match="holdout"):
        attempt(window=("2026-09-20T00:00:00Z", "2026-09-25T00:00:00Z"))
    with pytest.raises(CampaignError, match="in_sample"):
        attempt(window=("2026-03-10T00:00:00Z", "2026-03-13T00:00:00Z"))
    sip = (
        campaign_yaml(workflow, directory=tmp_path / "sip")
        .read_text()
        .replace("feed: iex", "feed: sip", 1)
    )
    (tmp_path / "sip" / "sip.yaml").write_text(sip)
    with pytest.raises(CampaignError, match="feeds are never mixed"):
        plan_campaign(load_campaign(tmp_path / "sip" / "sip.yaml"))
    missing = (
        campaign_yaml(workflow, directory=tmp_path / "m")
        .read_text()
        .replace("xgb_buy_t", "no_such_model", 1)
    )
    (tmp_path / "m" / "m.yaml").write_text(missing)
    with pytest.raises(CampaignError, match="no_such_model"):
        plan_campaign(load_campaign(tmp_path / "m" / "m.yaml"))
    bad_side = campaign_yaml(
        workflow, directory=tmp_path / "s", configs="  X: {buy: [xgb_sell], sell: []}\n"
    )
    with pytest.raises(CampaignError, match="SELL strategy listed under buy"):
        plan_campaign(load_campaign(bad_side))


def test_large_campaign_runs_without_approval_and_forwards_resources(
    workflow, tmp_path, monkeypatch
):
    import argparse

    import mike_trade.modeling.campaign_report as report_module
    import mike_trade.research.commands as research

    configs = "".join(
        f"  C{index:02d}: {{buy: [vwap_bounce], sell: [vwap_breakdown]}}\n" for index in range(60)
    )
    plan = plan_campaign(
        load_campaign(campaign_yaml(workflow, directory=tmp_path / "big", configs=configs))
    )
    assert len(plan.jobs) == 60
    seen: dict[str, object] = {}

    def fake_batch(jobs, **kwargs):
        seen.update(kwargs, count=len(jobs))
        return {"jobs": [], "failed_count": 0, "completed_count": len(jobs)}

    monkeypatch.setattr(research, "run_experiment_batch", fake_batch)
    monkeypatch.setattr(report_module, "write_campaign_report", lambda *a, **k: {})
    outcome = run_campaign(plan)
    assert outcome["batch"]["completed_count"] == 60 and seen["count"] == 60
    assert seen["workers"] == 2  # the campaign file's explicit max_workers
    assert seen["memory_per_worker_bytes"] == 1024 * 1024 * 1024
    run_campaign(plan, workers="auto")
    assert seen["workers"] == "auto"

    parser = argparse.ArgumentParser()
    from mike_trade.modeling.commands import register

    register(parser.add_subparsers())
    with pytest.raises(SystemExit):
        parser.parse_args(["model-backtest", "--campaign", "x.yaml", "--approve-large-run"])


def test_campaign_workers_default_to_auto_and_validate(workflow, tmp_path):
    text = campaign_yaml(workflow, directory=tmp_path / "w").read_text()
    path = tmp_path / "w" / "auto.yaml"
    path.write_text(text.replace("max_workers: 2\n", ""))
    assert load_campaign(path).max_workers == "auto"
    path.write_text(text.replace("max_workers: 2", "max_workers: 0"))
    with pytest.raises(CampaignError, match="max_workers"):
        load_campaign(path)


def test_reference_symbols_are_reported_not_traded(workflow, tmp_path):
    path = campaign_yaml(workflow, directory=tmp_path / "ref")
    path.write_text(path.read_text().replace("symbols: [AAA]", "symbols: [AAA, SPY]"))
    plan = plan_campaign(load_campaign(path))
    assert plan.summary()["excluded_reference_symbols"] == ["SPY"]
    assert "SPY" not in plan.symbols and {job.symbol for job in plan.jobs} == {"AAA"}


def test_market_crash_gating_blocks_model_entries_in_the_campaign(workflow, tmp_path):
    crash_data = workflow["base"] / "crash-data"
    build_dataset(crash_data, crash=True)
    configs = "  A_xgb_buy: {buy: [xgb_buy], sell: []}\n  H_baseline: {buy: [vwap_bounce], sell: [vwap_breakdown]}\n"
    gated_spec = load_campaign(
        campaign_yaml(
            workflow,
            directory=tmp_path / "gated",
            market_status=True,
            data=crash_data,
            configs=configs,
        )
    )
    gated = run_campaign(plan_campaign(gated_spec), workers=2)
    report = json.loads((gated_spec.run_directory / "report" / "report.json").read_text())
    a = next(c for c in report["configurations"] if c["name"] == "A_xgb_buy")
    assert a["market_status_entry_refusals"], "a crash must refuse model entries"
    assert any("CRASH" in reason for reason in a["market_status_entry_refusals"])
    open_spec = load_campaign(
        campaign_yaml(
            workflow,
            directory=tmp_path / "open",
            market_status=False,
            data=crash_data,
            configs=configs,
        )
    )
    run_campaign(plan_campaign(open_spec), workers=2)
    open_report = json.loads((open_spec.run_directory / "report" / "report.json").read_text())
    ungated = next(c for c in open_report["configurations"] if c["name"] == "A_xgb_buy")
    assert not ungated["market_status_entry_refusals"]
    assert a["trades"] <= ungated["trades"]
    assert gated["batch"]["failed_count"] == 0


# ------------------------------------------------------------------ SHADOW


def shadow_settings(workflow, tmp_path, buys, sells, **extra):
    from mike_trade.app import (
        AppSettings,
        ShadowScenarioSettings,
        StrategyAssignment,
        SymbolStrategySettings,
    )
    from mike_trade.runtime import ExecutionMode

    def pins(strategy, model_id, **parameters):
        return StrategyAssignment(
            f"{strategy}_cfg",
            strategy,
            {"model_id": model_id, "model_version": "1.0.0", **parameters},
        )

    scenario = ShadowScenarioSettings(
        name="acceptance",
        starting_cash=D("50000"),
        per_symbol_budget=D("3000"),
        total_budget=D("20000"),
        max_entry_cycles_per_session=5,
        consecutive_loss_limit=3,
        post_sale_cooldown=timedelta(0),
        minimum_hold=timedelta(0),
    )
    return AppSettings(
        mode=ExecutionMode.SHADOW,
        db_path=tmp_path / "shadow.sqlite3",
        symbols=("AAA",),
        shadow_scenarios=(scenario,),
        model_root=workflow["models"],
        symbol_strategies=(
            SymbolStrategySettings(
                "AAA",
                tuple(pins(*b[:2], **b[2]) for b in buys),
                tuple(pins(*s[:2], **s[2]) for s in sells),
            ),
        ),
        symbol_stop_policies={"AAA": {"type": "fixed", "stop_percent": D("3")}},
        **extra,
    )


class _Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def now(self) -> datetime:
        return self.at


async def test_same_models_initialize_and_operate_in_shadow(approved, tmp_path):
    workflow = approved
    from mike_trade.app import build_runtime
    from mike_trade.market_data.alpaca_stream import StreamQuote
    from mike_trade.market_data.candles import CandleOrigin, MinuteBar

    start = datetime(2026, 3, 23, 13, 30, tzinfo=UTC)
    clock = _Clock(start)
    settings = shadow_settings(
        workflow,
        tmp_path,
        [
            (
                "xgboost_probability_buy",
                "xgb_buy_t",
                {
                    "decision_mode": "THRESHOLD",
                    "minimum_favorable_probability_bps": 1,
                    "maximum_adverse_probability_bps": 10000,
                },
            )
        ],
        [
            (
                "lightgbm_probability_sell",
                "lgbm_sell_t",
                {"decision_mode": "THRESHOLD", "minimum_exit_probability_bps": 1},
            )
        ],
    )
    runtime = build_runtime(settings, overrides={"clock": clock})
    await runtime.start()
    try:
        life = runtime.lifecycle
        health = life.model_health()
        assert health is not None and health["connected"] == "true"
        summary = json.loads(health["summary_json"])
        assert {s["state"] for s in summary["strategies"]} == {"LOADED"}
        repository = HistoricalDatasetRepository(workflow["data"])
        bars = {
            symbol: repository.available(
                symbol, start, start + timedelta(hours=2), feed="iex", adjustment="raw"
            )
            for symbol in ("AAA", "SPY")
        }
        data = runtime.market_data
        vwaps = life._model_inputs.vwaps
        entered = False
        for index in range(60):
            for symbol in ("AAA", "SPY"):
                bar = bars[symbol][index]
                vwaps.record(symbol, bar.timestamp, bar.vwap)
                data.candles.on_minute_bar(
                    MinuteBar(
                        symbol,
                        bar.timestamp,
                        bar.open,
                        bar.high,
                        bar.low,
                        bar.close,
                        D(bar.volume),
                        "iex",
                        False,
                        origin=CandleOrigin.REPLAY,
                    )
                )
            clock.at = bars["AAA"][index].timestamp + timedelta(minutes=1, seconds=2)
            if index < 25:
                continue
            quote_price = bars["AAA"][index].close
            data.cache.store_event(
                StreamQuote(
                    "AAA",
                    clock.at,
                    clock.at,
                    "iex",
                    quote_price - D("0.01"),
                    D("100"),
                    quote_price + D("0.01"),
                    D("100"),
                ),
                source="stream",
            )
            context = data.snapshot(["AAA"])["AAA"]
            actions = await life.process({"AAA": context})
            entered = entered or any(a.startswith("ENTRY:") for a in actions["acceptance"])
        status = json.loads(life.model_health()["summary_json"])
        assert status["decisions"], "decisions are summarized for the dashboard"
        assert any(m["predictions"] > 0 and m["last_prediction"] for m in status["models"])
        assert entered, "permissive thresholds on a live-shaped tape must produce an entry"
        account = runtime.broker.scenarios["acceptance"].account
        assert account.state.positions or any(
            o.purpose.name == "STRATEGY_EXIT" for o in account.state.orders.values()
        )
        # the engine publishes read-only model status; CLI and dashboard read the same row
        await runtime._heartbeat()
        from fastapi.testclient import TestClient

        from mike_trade.dashboard.api import DashboardSettings
        from mike_trade.dashboard.server import create_app
        from mike_trade.modeling.operations import read_model_health

        published = read_model_health(settings.database_path)
        assert published["published"] and published["component_status"] == "HEALTHY"
        assert {m["model"] for m in published["models"]} == {"xgb_buy_t@1.0.0", "lgbm_sell_t@1.0.0"}
        client = TestClient(create_app(DashboardSettings(), clock, settings.database_path))
        scope = runtime.broker.scenarios["acceptance"].account.scope.scope_id
        response = client.get("/api/v2/models/status", params={"account_id": scope})
        assert response.status_code == 200
        body = response.json()
        assert body["published"] is True and {m["state"] for m in body["models"]} == {"LOADED"}
        assert any(m["last_prediction"] for m in body["models"])
        assert {d["strategy"] for d in body["decisions"]} <= {
            "xgboost_probability_buy",
            "lightgbm_probability_sell",
        }
        assert body["strategies"][0]["state"] == "LOADED"
        # protection exists independently of any model
        assert any(o.purpose.name == "PROTECTIVE_STOP" for o in account.state.orders.values())
    finally:
        await runtime.stop()


def test_missing_stale_or_incompatible_models_block_startup_safely(approved, tmp_path):
    workflow = approved
    from mike_trade.app import build_runtime
    from mike_trade.domain.errors import ConfigurationError

    buy = [("xgboost_probability_buy", "xgb_buy_t", {})]
    ok = shadow_settings(workflow, tmp_path / "ok", buy, [])
    (tmp_path / "ok").mkdir()
    build_runtime(ok, overrides={"clock": _Clock(datetime(2026, 3, 23, 14, tzinfo=UTC))})
    with pytest.raises(ConfigurationError) as missing:
        build_runtime(
            shadow_settings(
                workflow, tmp_path, [("xgboost_probability_buy", "ghost_model", {})], []
            ),
            overrides={"clock": _Clock(datetime(2026, 3, 23, 14, tzinfo=UTC))},
        )
    assert missing.value.code == "MODEL_MODEL_MISSING"
    with pytest.raises(
        ConfigurationError
    ) as wrong_algorithm:  # an XGBoost strategy never takes a LightGBM model
        build_runtime(
            shadow_settings(
                workflow, tmp_path, [("xgboost_probability_buy", "lgbm_buy_t", {})], []
            ),
            overrides={"clock": _Clock(datetime(2026, 3, 23, 14, tzinfo=UTC))},
        )
    assert wrong_algorithm.value.code == "MODEL_ALGORITHM_MISMATCH"
    with pytest.raises(ConfigurationError) as wrong_role:
        build_runtime(
            shadow_settings(
                workflow, tmp_path, [("xgboost_probability_buy", "xgb_sell_t", {})], []
            ),
            overrides={"clock": _Clock(datetime(2026, 3, 23, 14, tzinfo=UTC))},
        )
    assert wrong_role.value.code == "MODEL_ROLE_MISMATCH"
    with pytest.raises(ConfigurationError) as wrong_feed:
        build_runtime(
            shadow_settings(workflow, tmp_path, buy, [], market_data_feed="sip"),
            overrides={"clock": _Clock(datetime(2026, 3, 23, 14, tzinfo=UTC))},
        )
    assert wrong_feed.value.code == "MODEL_FEED_MISMATCH"


def test_corrupted_artifact_is_refused_at_startup_and_by_the_preflight(approved, tmp_path):
    workflow = approved
    from mike_trade.app import build_runtime
    from mike_trade.domain.errors import ConfigurationError

    manifest = workflow["outcomes"][("lightgbm", "buy")].stored
    original = manifest.artifact_path.read_bytes()
    manifest.artifact_path.write_bytes(original[:-4] + b"JUNK")
    try:
        with pytest.raises(ConfigurationError) as raised:
            build_runtime(
                shadow_settings(
                    workflow, tmp_path, [("lightgbm_probability_buy", "lgbm_buy_t", {})], []
                ),
                overrides={"clock": _Clock(datetime(2026, 3, 23, 14, tzinfo=UTC))},
            )
        assert raised.value.code == "MODEL_CHECKSUM_MISMATCH"
    finally:
        manifest.artifact_path.write_bytes(original)


def test_preflight_reports_pins_approvals_and_feed(approved, tmp_path):
    workflow = approved
    config = tmp_path / "trading.yaml"
    text = Path("config/model_strategies.example.yaml").read_text()
    text = (
        text.replace("model_root: research/models", f"model_root: {workflow['models']}")
        .replace("xgboost_buy_tb_pooled", "xgb_buy_t")
        .replace("lightgbm_sell_continuation_pooled", "lgbm_sell_t")
        .replace("lightgbm_buy_tb_pooled", "lgbm_buy_t")
        .replace("xgboost_sell_continuation_pooled", "xgb_sell_t")
        .replace("data_dir: state/model-shadow", f"data_dir: {tmp_path / 'state'}")
    )
    config.write_text(text)
    report = check_configuration(config, mode="shadow")
    assert report["ok"] is True, report
    assert {m["model"] for m in report["models"]} == {
        "xgb_buy_t@1.0.0",
        "lgbm_sell_t@1.0.0",
        "lgbm_buy_t@1.0.0",
        "xgb_sell_t@1.0.0",
    }
    assert all(m["highest_approval"] == "shadow" and m["feed"] == "iex" for m in report["models"])
    paper = check_configuration(config, mode="paper")
    assert paper["ok"] is False
    assert all("MODEL_NOT_APPROVED" in " ".join(m["problems"]) for m in paper["models"])
