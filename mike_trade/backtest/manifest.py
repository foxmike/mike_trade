"""Immutable, content-addressed backtest run manifests.

Manifests and run results are stored in run-scoped files by :mod:`runner`; the
existing SQLite schema is deliberately unchanged for this task.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime, time
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


class PeriodRole(StrEnum):
    RESEARCH = "RESEARCH"
    DEV = "DEV"
    HOLDOUT = "HOLDOUT"


class HoldoutViolationError(ValueError):
    """A holdout access or retuning rule was violated."""


def canonical_json(value: Any) -> str:
    """Canonical JSON for manifest/config/result hashing; rejects floats."""
    def normalize(item: Any) -> Any:
        if isinstance(item, datetime):
            if item.tzinfo is None or item.utcoffset() is None:
                raise ValueError("datetimes must be timezone-aware")
            return item.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
        if isinstance(item, time):
            return item.isoformat(timespec="microseconds")
        if hasattr(item, "value") and isinstance(item.value, (str, int)):
            return item.value
        if isinstance(item, float):
            raise TypeError("floats are not allowed in canonical backtest values")
        if isinstance(item, Mapping):
            return {str(k): normalize(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [normalize(v) for v in item]
        if isinstance(item, (str, int, bool)) or item is None:
            return item
        if hasattr(item, "as_mapping"):
            return normalize(item.as_mapping())
        if isinstance(item, Decimal):
            if not item.is_finite():
                raise ValueError("non-finite Decimal is not canonical")
            return format(item, "f")
        if is_dataclass(item):
            return normalize({field.name: getattr(item, field.name) for field in fields(item)})
        raise TypeError(f"unsupported canonical value {type(item).__name__}")

    return json.dumps(normalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def config_hash(config: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(config).encode("utf-8")).hexdigest()


def code_revision(cwd: str | Path | None = None) -> dict[str, Any]:
    """Capture local git commit and dirty status without network access."""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=cwd, check=True, capture_output=True,
            text=True, timeout=2,
        ).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain"], cwd=cwd, check=True, capture_output=True,
            text=True, timeout=2,
        ).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        commit, dirty = "unknown", False
    return {"commit": commit, "dirty": dirty}


@dataclass(frozen=True, slots=True)
class HoldoutLock:
    """Operator authorization and locked research/window identity."""
    token: str
    research_config_hash: str
    window_start: datetime
    window_end: datetime
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.token or not self.enabled:
            raise HoldoutViolationError("holdout access requires an enabled explicit lock token")
        for name in ("window_start", "window_end"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
            object.__setattr__(self, name, value.astimezone(UTC))
        if self.window_end <= self.window_start:
            raise ValueError("holdout window must be bounded and non-empty")


@dataclass(frozen=True, slots=True)
class RunManifest:
    run_id: str
    created_at: datetime
    config_hash: str
    symbols: tuple[str, ...]
    window_start: datetime
    window_end: datetime
    period_role: PeriodRole
    code_revision: Mapping[str, Any]
    data_provenance: Mapping[str, Any]
    data_revision: str
    data_vintage: str
    dataset_hash: str
    starting_capital: str
    fee_assumptions: Mapping[str, Any]
    slippage_assumptions: Mapping[str, Any]
    fill_assumptions: Mapping[str, Any]
    ambiguity_assumptions: Mapping[str, Any]
    seed: int
    strategies: tuple[Mapping[str, str], ...]
    holdout_access: Mapping[str, Any] | None = None
    # run_id is the deterministic experiment/configuration identity. These
    # fields identify one completed invocation and are optional for legacy files.
    execution_id: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    run_purpose: str | None = None
    configuration: Mapping[str, Any] | None = None
    completion_status: str = "COMPLETED"

    def __post_init__(self) -> None:
        if not self.run_id or not self.config_hash:
            raise ValueError("run_id and config_hash are required")
        if not isinstance(self.period_role, PeriodRole):
            object.__setattr__(self, "period_role", PeriodRole(self.period_role))
        for name in ("created_at", "window_start", "window_end", "started_at", "completed_at"):
            dt = getattr(self, name)
            if dt is None:
                continue
            if dt.tzinfo is None or dt.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
            object.__setattr__(self, name, dt.astimezone(UTC))
        if self.window_end <= self.window_start:
            raise ValueError("window must be bounded and non-empty")
        if not 1 <= len(self.symbols) <= 10:
            raise ValueError("manifest requires 1-10 symbols")
        object.__setattr__(self, "symbols", tuple(sorted({s.upper() for s in self.symbols})))
        for name in ("code_revision", "data_provenance", "fee_assumptions", "slippage_assumptions",
                     "fill_assumptions", "ambiguity_assumptions", "holdout_access",
                     "configuration"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, MappingProxyType(dict(value)))
        object.__setattr__(self, "strategies", tuple(MappingProxyType(dict(v)) for v in self.strategies))
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ValueError("seed must be int")

    def canonical(self) -> str:
        return canonical_json({field.name: getattr(self, field.name) for field in fields(self)})

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()

    @classmethod
    def from_json(cls, value: str) -> "RunManifest":
        raw = json.loads(value)
        for field in ("created_at", "window_start", "window_end", "started_at", "completed_at"):
            if raw.get(field) is not None:
                raw[field] = datetime.fromisoformat(raw[field].replace("Z", "+00:00"))
        raw["period_role"] = PeriodRole(raw["period_role"])
        raw["symbols"] = tuple(raw["symbols"])
        raw["strategies"] = tuple(raw["strategies"])
        return cls(**raw)


def write_manifest(manifest: RunManifest, directory: str | Path) -> Path:
    """Create once; exclusive creation rejects any existing run id."""
    target = Path(directory) / f"{manifest.run_id}.manifest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = manifest.canonical()
    with target.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return target
