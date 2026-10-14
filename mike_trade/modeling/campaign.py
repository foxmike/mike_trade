"""Reproducible model-driven backtest campaigns on the existing research runner.

A campaign file (versioned YAML) names pinned models, the strategy collections to compare, the
feed, the development window, the frozen universe, costs and the protective stop.  It expands to
ordinary :class:`ExperimentSpec` jobs — one per (configuration, symbol) — that are executed by the
existing :func:`run_experiment_batch` / ``BacktestRunner``.  This module adds no simulator and no
hard-coded entry/exit pair: every configuration is just a list of registered strategy ids, and
the existing arbitration, next-bar execution, stops, market-status gate and trade ledger apply.

Safety properties enforced here, before any job runs:

* the window must not touch the reserved final holdout;
* the feed is explicit and every snapshot (primary and reference) must be of that feed;
* each model is pinned by exact id/version *and* artifact checksum, resolved from the immutable
  repository (a campaign can never pick "the latest model");
* a window that overlaps a model's training or tuning data is refused unless the campaign says
  ``allow_in_sample: true``, and is labelled in every report either way;
* a large campaign needs explicit approval at the command line.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from mike_trade.modeling.artifacts import FileModelRepository, ModelArtifactManifest
from mike_trade.modeling.model_reports import classify_window
from mike_trade.modeling.splits import (
    ExperimentPurpose,
    HoldoutAccessError,
    TimeRange,
    authorize_range,
)

__all__ = [
    "CampaignError",
    "ModelPin",
    "CampaignConfiguration",
    "CampaignSpec",
    "CampaignPlan",
    "load_campaign",
    "plan_campaign",
    "run_campaign",
]

_ALLOWED_KEYS = {
    "schema_version",
    "campaign_id",
    "description",
    "feed",
    "universe_file",
    "universe_id",
    "symbols",
    "exclude_symbols",
    "window",
    "dataset_root",
    "model_root",
    "output_root",
    "seed",
    "starting_capital",
    "execution",
    "protective_stop",
    "end_of_day_close",
    "max_entry_cycles_per_session",
    "allow_in_sample",
    "max_workers",
    "worker_memory_mb",
    "models",
    "configurations",
    "strategy_parameters",
    "market_status_enabled",
}
_MODEL_STRATEGIES = {
    "xgboost_probability_buy",
    "xgboost_probability_sell",
    "lightgbm_probability_buy",
    "lightgbm_probability_sell",
}


class CampaignError(ValueError):
    """The campaign file or its pinned inputs are invalid (nothing was run)."""


@dataclass(frozen=True, slots=True)
class ModelPin:
    name: str
    strategy: str
    model_id: str
    model_version: str
    parameters: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class CampaignConfiguration:
    name: str
    buy: tuple[str, ...]
    sell: tuple[str, ...]
    description: str = ""


@dataclass(frozen=True, slots=True)
class CampaignSpec:
    campaign_id: str
    description: str
    feed: str
    universe_file: Path
    universe_id: str
    symbols: tuple[str, ...]
    exclude_symbols: tuple[str, ...]
    window: TimeRange
    dataset_root: Path
    model_root: Path
    output_root: Path
    seed: int
    starting_capital: str
    execution: Mapping[str, Any]
    protective_stop: Mapping[str, Any]
    end_of_day_close: Mapping[str, Any] | None
    max_entry_cycles_per_session: int
    allow_in_sample: bool
    market_status_enabled: bool
    max_workers: str | int
    worker_memory_mb: int
    models: Mapping[str, ModelPin]
    configurations: tuple[CampaignConfiguration, ...]
    strategy_parameters: Mapping[str, Mapping[str, Any]]
    config_sha256: str
    source: Path

    @property
    def run_directory(self) -> Path:
        return self.output_root / self.campaign_id / self.config_sha256[:12]


@dataclass(frozen=True, slots=True)
class CampaignPlan:
    spec: CampaignSpec
    symbols: tuple[str, ...]
    skipped: Mapping[str, str]
    manifests: Mapping[str, ModelArtifactManifest]
    sample_roles: Mapping[str, str]
    dataset_ids: Mapping[str, str]
    reference_dataset_ids: Mapping[str, str]
    jobs: tuple[Any, ...]
    #: Universe members used as pinned models' market-reference feed, so never traded as targets.
    excluded_reference_symbols: tuple[str, ...] = ()

    def summary(self) -> dict[str, Any]:
        return {
            "campaign_id": self.spec.campaign_id,
            "config_sha256": self.spec.config_sha256,
            "run_directory": str(self.spec.run_directory),
            "feed": self.spec.feed,
            "window": [self.spec.window.start.isoformat(), self.spec.window.end.isoformat()],
            "symbols": len(self.symbols),
            "excluded_reference_symbols": list(self.excluded_reference_symbols),
            "skipped_symbols": dict(self.skipped),
            "configurations": [item.name for item in self.spec.configurations],
            "jobs": len(self.jobs),
            "models": {
                name: {
                    "model": f"{m.model_id}@{m.model_version}",
                    "artifact_sha256": m.artifact_sha256,
                    "sample_role": self.sample_roles[name],
                }
                for name, m in self.manifests.items()
            },
            "reference_dataset_ids": dict(self.reference_dataset_ids),
        }


def _moment(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise CampaignError(f"{name} must be an ISO-8601 timestamp")
    if result.tzinfo is None:
        raise CampaignError(f"{name} must include a timezone")
    return result.astimezone(UTC)


def load_campaign(path: str | Path) -> CampaignSpec:
    source = Path(path)
    payload = source.read_bytes()
    raw = yaml.safe_load(payload)
    if not isinstance(raw, Mapping):
        raise CampaignError("campaign must be a YAML mapping")
    unknown = set(raw) - _ALLOWED_KEYS
    if unknown:
        raise CampaignError(f"unknown campaign keys: {sorted(unknown)}")
    if raw.get("schema_version") != 1:
        raise CampaignError("unsupported campaign schema_version")
    feed = str(raw.get("feed", "")).strip().lower()
    if feed not in {"iex", "sip"}:
        raise CampaignError("feed must be explicitly 'iex' or 'sip'")
    window_raw = raw.get("window")
    if not isinstance(window_raw, Mapping):
        raise CampaignError("window {start, end} is required")
    window = TimeRange(
        _moment(window_raw["start"], "window.start"), _moment(window_raw["end"], "window.end")
    )
    campaign_id = str(raw.get("campaign_id", ""))
    if not campaign_id or not all(ch.isalnum() or ch in "_-" for ch in campaign_id):
        raise CampaignError("campaign_id must be a non-empty slug")
    models: dict[str, ModelPin] = {}
    for name, item in dict(raw.get("models") or {}).items():
        if not isinstance(item, Mapping):
            raise CampaignError(f"model {name!r} must be a mapping")
        extra = set(item) - {"strategy", "model_id", "model_version", "parameters"}
        if extra:
            raise CampaignError(f"model {name!r} has unknown keys {sorted(extra)}")
        strategy = str(item.get("strategy", ""))
        if strategy not in _MODEL_STRATEGIES:
            raise CampaignError(
                f"model {name!r}: strategy must be one of {sorted(_MODEL_STRATEGIES)}"
            )
        params = dict(item.get("parameters") or {})
        for forbidden in ("model_id", "model_version", "artifact_sha256"):
            if forbidden in params:
                raise CampaignError(
                    f"model {name!r}: {forbidden} is set by the pin, not parameters"
                )
        models[str(name)] = ModelPin(
            str(name), strategy, str(item["model_id"]), str(item["model_version"]), params
        )
    configs: list[CampaignConfiguration] = []
    for name, item in dict(raw.get("configurations") or {}).items():
        if not isinstance(item, Mapping) or set(item) - {"buy", "sell", "description"}:
            raise CampaignError(f"configuration {name!r} accepts only buy, sell, description")
        buy, sell = tuple(map(str, item.get("buy") or ())), tuple(map(str, item.get("sell") or ()))
        if not buy:
            raise CampaignError(f"configuration {name!r} needs at least one BUY strategy")
        label = str(name)
        if not label.replace("_", "").replace("-", "").isalnum():
            raise CampaignError(f"invalid configuration name {label!r}")
        configs.append(CampaignConfiguration(label, buy, sell, str(item.get("description", ""))))
    if not configs:
        raise CampaignError("at least one configuration is required")
    if len({c.name for c in configs}) != len(configs):
        raise CampaignError("configuration names must be unique")
    eod = raw.get("end_of_day_close")
    return CampaignSpec(
        campaign_id=campaign_id,
        description=str(raw.get("description", "")),
        feed=feed,
        universe_file=Path(str(raw["universe_file"])),
        universe_id=str(raw["universe_id"]),
        symbols=tuple(str(s).upper() for s in raw.get("symbols") or ()),
        exclude_symbols=tuple(str(s).upper() for s in raw.get("exclude_symbols") or ()),
        window=window,
        dataset_root=Path(str(raw.get("dataset_root", "research/data"))),
        model_root=Path(str(raw.get("model_root", "research/models"))),
        output_root=Path(str(raw.get("output_root", "research/model_campaigns"))),
        seed=int(raw.get("seed", 271828)),
        starting_capital=str(raw.get("starting_capital", "10000")),
        execution=dict(raw.get("execution") or {}),
        protective_stop=dict(
            raw.get("protective_stop") or {"type": "native_trailing", "trail_percent": "1.5"}
        ),
        end_of_day_close=None if eod is None else dict(eod),
        max_entry_cycles_per_session=int(raw.get("max_entry_cycles_per_session", 10)),
        allow_in_sample=bool(raw.get("allow_in_sample", False)),
        market_status_enabled=bool(raw.get("market_status_enabled", False)),
        max_workers=_workers(raw.get("max_workers", "auto")),
        worker_memory_mb=_positive(raw.get("worker_memory_mb", 1024), "worker_memory_mb"),
        models=models,
        configurations=tuple(configs),
        strategy_parameters={
            str(k): dict(v) for k, v in dict(raw.get("strategy_parameters") or {}).items()
        },
        config_sha256=hashlib.sha256(payload).hexdigest(),
        source=source,
    )


def _positive(value: object, name: str) -> int:
    try:
        number = int(str(value))
    except ValueError as exc:
        raise CampaignError(f"{name} must be a positive integer") from exc
    if number < 1:
        raise CampaignError(f"{name} must be a positive integer")
    return number


def _workers(value: object) -> str | int:
    if str(value).strip().lower() == "auto":
        return "auto"
    return _positive(value, "max_workers (a positive integer or 'auto')")


def _universe_symbols(spec: CampaignSpec) -> tuple[str, ...]:
    data = json.loads(spec.universe_file.read_text(encoding="utf-8"))
    if data.get("universe_id") != spec.universe_id:
        raise CampaignError(
            f"universe file is {data.get('universe_id')!r}, campaign expects {spec.universe_id!r}"
        )
    universe = tuple(dict.fromkeys(str(item["symbol"]).upper() for item in data.get("symbols", ())))
    if spec.symbols:
        outside = sorted(set(spec.symbols) - set(universe))
        if outside:
            raise CampaignError(f"symbols outside the frozen universe: {outside}")
        return spec.symbols
    return universe


def _snapshot_for(
    snapshots: Sequence[Any], symbol: str, feed: str, window: TimeRange
) -> Any | None:
    matches = [
        item
        for item in snapshots
        if item.symbol == symbol
        and item.feed == feed
        and item.adjustment == "raw"
        and item.timeframe == "1m"
        and datetime.fromisoformat(item.start) <= window.start
        and datetime.fromisoformat(item.end) >= window.end
    ]
    return max(matches, key=lambda item: (item.acquired_at, item.dataset_id)) if matches else None


def plan_campaign(spec: CampaignSpec, *, skip_missing: bool = False) -> CampaignPlan:
    """Validate everything and expand to jobs; touches no result directory."""
    from mike_trade.research.commands import ExperimentSpec, _research_registry
    from mike_trade.research.datasets import HistoricalDatasetRepository

    try:
        authorize_range(
            spec.window, ExperimentPurpose.DEVELOPMENT, experiment_hash=spec.config_sha256
        )
    except HoldoutAccessError as exc:
        raise CampaignError(
            f"{exc}. Holdout evaluation is a separate, explicitly authorized operation"
        ) from exc
    registry = _research_registry()
    repository = FileModelRepository(spec.model_root)
    manifests: dict[str, ModelArtifactManifest] = {}
    roles: dict[str, str] = {}
    reference: set[str] = set()
    for name, pin in spec.models.items():
        try:
            manifest, _ = repository.get(pin.model_id, pin.model_version)
        except (FileNotFoundError, ValueError) as exc:
            raise CampaignError(
                f"model {name!r} ({pin.model_id}@{pin.model_version}): {exc}"
            ) from exc
        if manifest.algorithm != registry.get(pin.strategy).spec.strategy_id.split("_")[0]:
            raise CampaignError(
                f"model {name!r}: {pin.strategy} cannot use a {manifest.algorithm} model"
            )
        if manifest.feed != spec.feed:
            raise CampaignError(
                f"model {name!r} was trained on {manifest.feed.upper()} but the campaign feed is "
                f"{spec.feed.upper()}; feeds are never mixed"
            )
        wanted = "sell" if pin.strategy.endswith("_sell") else "buy"
        actual = "sell" if manifest.feature_schema.feature_id == "intraday_position" else "buy"
        if wanted != actual:
            raise CampaignError(f"model {name!r}: {pin.strategy} cannot use a {actual} model")
        role = classify_window(manifest, spec.window.start, spec.window.end)
        if role == "holdout":
            raise CampaignError("the window overlaps the reserved final holdout")
        if role in {"in_sample", "tuning_overlap"} and not spec.allow_in_sample:
            raise CampaignError(
                f"model {name!r}: window is {role} for {pin.model_id}@{pin.model_version}; "
                "choose a window after its fit/tuning data or set allow_in_sample: true"
            )
        manifests[name] = manifest
        roles[name] = role
        reference.update(manifest.feature_schema.required_reference_symbols)

    universe = _universe_symbols(spec)
    candidates = tuple(s for s in universe if s not in set(spec.exclude_symbols) | reference)
    excluded_reference = tuple(s for s in universe if s in reference)
    snapshots = HistoricalDatasetRepository(spec.dataset_root).snapshots()
    dataset_ids: dict[str, str] = {}
    skipped: dict[str, str] = {}
    for symbol in candidates:
        found = _snapshot_for(snapshots, symbol, spec.feed, spec.window)
        if found is None:
            skipped[symbol] = f"no {spec.feed.upper()} 1m raw snapshot covers the window"
        else:
            dataset_ids[symbol] = found.dataset_id
    if skipped and not skip_missing:
        raise CampaignError(
            f"{len(skipped)} symbol(s) lack {spec.feed.upper()} coverage "
            f"({', '.join(sorted(skipped)[:8])}…); fix the data or pass --skip-missing"
        )
    if not dataset_ids:
        raise CampaignError("no symbol has a usable snapshot")
    reference_ids: dict[str, str] = {}
    status_symbols = {"SPY", "QQQ", "IWM"} if spec.market_status_enabled else set()
    for symbol in sorted(reference | status_symbols):
        found = _snapshot_for(snapshots, symbol, spec.feed, spec.window)
        if found is None:
            raise CampaignError(f"reference symbol {symbol} has no {spec.feed.upper()} snapshot")
        reference_ids[symbol] = found.dataset_id

    # expand configurations -> per-job strategy parameters
    jobs: list[Any] = []
    for configuration in spec.configurations:
        parameters: dict[str, dict[str, Any]] = {}
        sides: dict[str, list[str]] = {"buy": [], "sell": []}
        for side in ("buy", "sell"):
            for item in getattr(configuration, side):
                if item in spec.models:
                    pin = spec.models[item]
                    manifest = manifests[item]
                    strategy_id = pin.strategy
                    values = {
                        **pin.parameters,
                        "model_id": pin.model_id,
                        "model_version": pin.model_version,
                        "artifact_sha256": manifest.artifact_sha256,
                    }
                else:
                    strategy_id = item
                    values = dict(spec.strategy_parameters.get(item, {}))
                try:
                    cls = registry.get(strategy_id)
                except Exception as exc:
                    raise CampaignError(f"{configuration.name}: unknown strategy {item!r}") from exc
                expected = "buy" if cls.spec.side.value == "BUY" else "sell"
                if cls.spec.side.value not in {"BUY", "SELL"} or expected != side:
                    raise CampaignError(
                        f"{configuration.name}: {strategy_id} is a {cls.spec.side.value} strategy "
                        f"listed under {side}"
                    )
                if strategy_id in parameters:
                    raise CampaignError(
                        f"{configuration.name}: {strategy_id} appears twice; a strategy may "
                        "appear once per configuration"
                    )
                try:
                    registry.validate_parameters(strategy_id, values)
                except Exception as exc:
                    raise CampaignError(f"{configuration.name}: {strategy_id}: {exc}") from exc
                parameters[strategy_id] = values
                sides[side].append(strategy_id)
        needs_model = any(s in _MODEL_STRATEGIES for s in parameters)
        needs_references = needs_model or spec.market_status_enabled
        for symbol in sorted(dataset_ids):
            jobs.append(
                ExperimentSpec(
                    symbol=symbol,
                    strategy=configuration.name,
                    start=spec.window.start.isoformat(),
                    end=spec.window.end.isoformat(),
                    seed=spec.seed,
                    dataset_id=dataset_ids[symbol],
                    feed=spec.feed,
                    parameters={k: dict(v) for k, v in parameters.items()},
                    starting_capital=spec.starting_capital,
                    execution=dict(spec.execution) or None,
                    buy_strategies=tuple(sides["buy"]),
                    sell_strategies=tuple(sides["sell"]),
                    protective_stop=dict(spec.protective_stop),
                    end_of_day_close=spec.end_of_day_close,
                    max_entry_cycles_per_session=spec.max_entry_cycles_per_session,
                    model_root=str(spec.model_root.resolve()) if needs_model else None,
                    reference_dataset_ids=dict(reference_ids) if needs_references else None,
                    market_status_enabled=spec.market_status_enabled,
                )
            )
    return CampaignPlan(
        spec,
        tuple(sorted(dataset_ids)),
        skipped,
        manifests,
        roles,
        dataset_ids,
        reference_ids,
        tuple(jobs),
        excluded_reference,
    )


def run_campaign(
    plan: CampaignPlan,
    *,
    workers: str | int | None = None,
) -> dict[str, Any]:
    """Execute the planned jobs on the existing batch runner and write the report set."""
    from mike_trade.modeling.campaign_report import write_campaign_report
    from mike_trade.research.commands import run_experiment_batch

    spec = plan.spec
    directory = spec.run_directory
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(spec.source, directory / "campaign.yaml")  # the exact reproducible input
    (directory / "plan.json").write_text(
        json.dumps(plan.summary(), sort_keys=True, indent=2, default=str) + "\n", encoding="utf-8"
    )
    batch = run_experiment_batch(
        list(plan.jobs),
        workers=workers if workers is not None else spec.max_workers,
        root=str(spec.dataset_root),
        results=str(directory),
        batch_file=str(directory / "batch.json"),
        mp_context=multiprocessing.get_context("spawn"),
        memory_per_worker_bytes=spec.worker_memory_mb * 1024 * 1024,
    )
    paths = write_campaign_report(plan, batch, directory / "report")
    return {"batch": {k: v for k, v in batch.items() if k != "jobs"}, "report": paths}
