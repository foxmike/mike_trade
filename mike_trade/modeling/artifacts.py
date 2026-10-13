"""Strict, immutable model manifests and checksum-verified native artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

from mike_trade.backtest.manifest import canonical_json
from mike_trade.modeling.contracts import (
    DatasetSource,
    FeatureSchema,
    LabelSchema,
    LearningTask,
    identifier,
    semver,
    sha256,
    utc,
)
from mike_trade.modeling.splits import DEVELOPMENT_START, FINAL_HOLDOUT, TimeRange

_FORMAT = re.compile(r"[a-z][a-z0-9_]{0,31}")
_UNSAFE_FORMAT_PARTS = ("pickle", "pkl", "joblib", "cloudpickle", "dill")


def _feature_schema_mapping(schema: FeatureSchema) -> dict[str, object]:
    return {
        "feature_id": schema.feature_id,
        "version": schema.version,
        "names": schema.names,
        "required_reference_symbols": schema.required_reference_symbols,
        "timeframe": schema.timeframe,
    }


def _label_schema_mapping(schema: LabelSchema) -> dict[str, object]:
    return {
        "label_id": schema.label_id,
        "version": schema.version,
        "task": schema.task,
        "horizon_bars": schema.horizon_bars,
        "parameters": dict(schema.parameters),
    }


def schema_fingerprint(schema: FeatureSchema | LabelSchema) -> str:
    mapping = (
        _feature_schema_mapping(schema)
        if isinstance(schema, FeatureSchema)
        else _label_schema_mapping(schema)
    )
    return hashlib.sha256(canonical_json(mapping).encode()).hexdigest()


def _freeze_str_mapping(value: Mapping[str, object], field_name: str) -> Mapping[str, str]:
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"{field_name} keys must be non-empty strings")
        if isinstance(item, Decimal):
            if not item.is_finite():
                raise ValueError(f"{field_name} values must be finite")
            result[key] = str(item)
        elif isinstance(item, (str, int, bool)):
            result[key] = str(item)
        else:
            raise ValueError(f"{field_name} values must be scalar and JSON-safe")
    return MappingProxyType(result)


@dataclass(frozen=True, slots=True)
class ModelArtifactManifest:
    manifest_schema_version: int
    model_id: str
    model_version: str
    created_at: datetime
    adapter_id: str
    adapter_version: str
    artifact_format: str
    artifact_sha256: str
    artifact_size: int
    feature_schema: FeatureSchema
    feature_schema_sha256: str
    label_schema: LabelSchema
    label_schema_sha256: str
    dataset_sources: tuple[DatasetSource, ...]
    feed: str
    timeframe: str
    adjustment: str
    universe_id: str
    training_ranges: tuple[TimeRange, ...]
    validation_ranges: tuple[TimeRange, ...]
    validation_method: str
    execution_assumptions: Mapping[str, object]
    random_seed: int
    software_versions: Mapping[str, object]
    training_config_sha256: str
    validation_metrics: Mapping[str, object]
    holdout_evaluated: bool = False

    def __post_init__(self) -> None:
        if self.manifest_schema_version != 1:
            raise ValueError("unsupported model manifest schema version")
        object.__setattr__(self, "model_id", identifier(self.model_id, "model_id"))
        object.__setattr__(self, "model_version", semver(self.model_version, "model_version"))
        object.__setattr__(self, "created_at", utc(self.created_at, "created_at"))
        object.__setattr__(self, "adapter_id", identifier(self.adapter_id, "adapter_id"))
        object.__setattr__(self, "adapter_version", semver(self.adapter_version, "adapter_version"))
        if _FORMAT.fullmatch(self.artifact_format) is None or any(
            unsafe in self.artifact_format.lower() for unsafe in _UNSAFE_FORMAT_PARTS
        ):
            raise ValueError("artifact_format must name a safe native, JSON, or tensor format")
        object.__setattr__(self, "artifact_sha256", sha256(self.artifact_sha256, "artifact_sha256"))
        if isinstance(self.artifact_size, bool) or self.artifact_size < 1:
            raise ValueError("artifact_size must be a positive int")
        if not isinstance(self.feature_schema, FeatureSchema):
            raise ValueError("feature_schema must be FeatureSchema")
        if not isinstance(self.label_schema, LabelSchema):
            raise ValueError("label_schema must be LabelSchema")
        if self.feature_schema_sha256 != schema_fingerprint(self.feature_schema):
            raise ValueError("feature schema fingerprint mismatch")
        if self.label_schema_sha256 != schema_fingerprint(self.label_schema):
            raise ValueError("label schema fingerprint mismatch")
        if not self.dataset_sources:
            raise ValueError("dataset_sources must be non-empty")
        identities = {
            (source.feed, source.timeframe, source.adjustment) for source in self.dataset_sources
        }
        if len(identities) != 1 or identities.pop() != (
            self.feed,
            self.timeframe,
            self.adjustment,
        ):
            raise ValueError("manifest provenance conflicts with dataset sources")
        if not self.universe_id:
            raise ValueError("universe_id is required")
        if not self.training_ranges or not self.validation_ranges:
            raise ValueError("exact training and validation ranges are required")
        every_range = (*self.training_ranges, *self.validation_ranges)
        if any(value.start < DEVELOPMENT_START for value in every_range):
            raise ValueError("model manifest ranges cannot precede the development period")
        if any(value.overlaps(FINAL_HOLDOUT) for value in every_range):
            raise ValueError("training manifests cannot include the reserved final holdout")
        for ranges in (self.training_ranges, self.validation_ranges):
            ordered = sorted(ranges, key=lambda value: value.start)
            if any(left.overlaps(right) for left, right in zip(ordered, ordered[1:], strict=False)):
                raise ValueError("manifest ranges within one partition must not overlap")
        if any(
            training.overlaps(validation)
            for training in self.training_ranges
            for validation in self.validation_ranges
        ):
            raise ValueError("training and validation ranges must not overlap")
        if not self.validation_method:
            raise ValueError("validation_method is required")
        if isinstance(self.random_seed, bool) or not isinstance(self.random_seed, int):
            raise ValueError("random_seed must be int")
        object.__setattr__(
            self,
            "execution_assumptions",
            _freeze_str_mapping(self.execution_assumptions, "execution_assumptions"),
        )
        object.__setattr__(
            self,
            "software_versions",
            _freeze_str_mapping(self.software_versions, "software_versions"),
        )
        object.__setattr__(
            self,
            "validation_metrics",
            _freeze_str_mapping(self.validation_metrics, "validation_metrics"),
        )
        object.__setattr__(
            self,
            "training_config_sha256",
            sha256(self.training_config_sha256, "training_config_sha256"),
        )
        if self.holdout_evaluated:
            raise ValueError("training manifests cannot claim final holdout evaluation")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "manifest_schema_version": self.manifest_schema_version,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "created_at": self.created_at,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "artifact_format": self.artifact_format,
            "artifact_sha256": self.artifact_sha256,
            "artifact_size": self.artifact_size,
            "feature_schema": _feature_schema_mapping(self.feature_schema),
            "feature_schema_sha256": self.feature_schema_sha256,
            "label_schema": _label_schema_mapping(self.label_schema),
            "label_schema_sha256": self.label_schema_sha256,
            "dataset_sources": [asdict(source) for source in self.dataset_sources],
            "feed": self.feed,
            "timeframe": self.timeframe,
            "adjustment": self.adjustment,
            "universe_id": self.universe_id,
            "training_ranges": [asdict(value) for value in self.training_ranges],
            "validation_ranges": [asdict(value) for value in self.validation_ranges],
            "validation_method": self.validation_method,
            "execution_assumptions": dict(self.execution_assumptions),
            "random_seed": self.random_seed,
            "software_versions": dict(self.software_versions),
            "training_config_sha256": self.training_config_sha256,
            "validation_metrics": dict(self.validation_metrics),
            "holdout_evaluated": self.holdout_evaluated,
        }

    def canonical(self) -> str:
        return canonical_json(self.to_mapping())

    @classmethod
    def from_json(cls, payload: str) -> ModelArtifactManifest:
        raw = json.loads(payload)
        expected = {
            "manifest_schema_version",
            "model_id",
            "model_version",
            "created_at",
            "adapter_id",
            "adapter_version",
            "artifact_format",
            "artifact_sha256",
            "artifact_size",
            "feature_schema",
            "feature_schema_sha256",
            "label_schema",
            "label_schema_sha256",
            "dataset_sources",
            "feed",
            "timeframe",
            "adjustment",
            "universe_id",
            "training_ranges",
            "validation_ranges",
            "validation_method",
            "execution_assumptions",
            "random_seed",
            "software_versions",
            "training_config_sha256",
            "validation_metrics",
            "holdout_evaluated",
        }
        if set(raw) != expected:
            raise ValueError("model manifest has missing or unknown fields")
        raw["created_at"] = datetime.fromisoformat(raw["created_at"].replace("Z", "+00:00"))
        feature = raw["feature_schema"]
        feature["names"] = tuple(feature["names"])
        feature["required_reference_symbols"] = tuple(feature["required_reference_symbols"])
        raw["feature_schema"] = FeatureSchema(**feature)
        label = raw["label_schema"]
        label["task"] = LearningTask(label["task"])
        raw["label_schema"] = LabelSchema(**label)
        sources = []
        for source in raw["dataset_sources"]:
            source["coverage_start"] = datetime.fromisoformat(
                source["coverage_start"].replace("Z", "+00:00")
            )
            source["coverage_end"] = datetime.fromisoformat(
                source["coverage_end"].replace("Z", "+00:00")
            )
            sources.append(DatasetSource(**source))
        raw["dataset_sources"] = tuple(sources)
        for field_name in ("training_ranges", "validation_ranges"):
            raw[field_name] = tuple(
                TimeRange(
                    datetime.fromisoformat(item["start"].replace("Z", "+00:00")),
                    datetime.fromisoformat(item["end"].replace("Z", "+00:00")),
                )
                for item in raw[field_name]
            )
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class StoredModel:
    manifest_path: Path
    artifact_path: Path
    manifest: ModelArtifactManifest


class FileModelRepository:
    """Content-addressed artifacts plus model-id/version-pinned immutable manifests."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.artifact_dir = self.root / "artifacts"
        self.manifest_dir = self.root / "manifests"

    def put(self, manifest: ModelArtifactManifest, artifact: bytes) -> StoredModel:
        if not isinstance(manifest, ModelArtifactManifest):
            raise TypeError("manifest must be ModelArtifactManifest")
        if not isinstance(artifact, bytes) or not artifact:
            raise ValueError("artifact must be non-empty bytes")
        digest = hashlib.sha256(artifact).hexdigest()
        if digest != manifest.artifact_sha256 or len(artifact) != manifest.artifact_size:
            raise ValueError("artifact bytes do not match the manifest")
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        model_manifest_dir = self.manifest_dir / manifest.model_id
        model_manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = model_manifest_dir / f"{manifest.model_version}.json"
        manifest_payload = (manifest.canonical() + "\n").encode()
        if manifest_path.exists() and manifest_path.read_bytes() != manifest_payload:
            raise FileExistsError(f"immutable artifact already exists: {manifest_path}")
        artifact_path = self.artifact_dir / digest / f"model.{manifest.artifact_format}"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_create(artifact_path, artifact)
        _atomic_create(manifest_path, manifest_payload)
        stored, stored_bytes = self.get(manifest.model_id, manifest.model_version)
        if stored.canonical() != manifest.canonical() or stored_bytes != artifact:
            raise ValueError("stored model verification failed")
        return StoredModel(manifest_path, artifact_path, stored)

    def get(self, model_id: str, version: str) -> tuple[ModelArtifactManifest, bytes]:
        model = identifier(model_id, "model_id")
        pinned = semver(version, "model_version")
        path = self.manifest_dir / model / f"{pinned}.json"
        manifest = ModelArtifactManifest.from_json(path.read_text(encoding="utf-8"))
        artifact_path = (
            self.artifact_dir / manifest.artifact_sha256 / f"model.{manifest.artifact_format}"
        )
        artifact = artifact_path.read_bytes()
        if len(artifact) != manifest.artifact_size:
            raise ValueError("model artifact size mismatch")
        if hashlib.sha256(artifact).hexdigest() != manifest.artifact_sha256:
            raise ValueError("model artifact checksum mismatch")
        return manifest, artifact


def _atomic_create(path: Path, payload: bytes) -> None:
    """Write/fsync then publish without replacement. Exact retries are idempotent."""
    fd, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise FileExistsError(f"immutable artifact already exists: {path}") from None
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
