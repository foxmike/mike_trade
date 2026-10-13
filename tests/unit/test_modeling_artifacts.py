from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mike_trade.modeling.artifacts import (
    FileModelRepository,
    ModelArtifactManifest,
    schema_fingerprint,
)
from mike_trade.modeling.contracts import (
    DatasetSource,
    FeatureSchema,
    LabelSchema,
    LearningTask,
)
from mike_trade.modeling.splits import TimeRange

NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _manifest(artifact: bytes, *, version: str = "1.0.0") -> ModelArtifactManifest:
    features = FeatureSchema("intraday_core", "1.0.0", ("return_1", "volume_mean_5"))
    labels = LabelSchema("forward_return", "1.0.0", LearningTask.REGRESSION, 5, {"entry_bps": "5"})
    source = DatasetSource(
        "dataset-1",
        "d" * 64,
        "AAA",
        "iex",
        "1m",
        "raw",
        datetime(2025, 10, 9, tzinfo=UTC),
        datetime(2026, 9, 21, tzinfo=UTC),
    )
    return ModelArtifactManifest(
        1,
        "pooled_intraday",
        version,
        NOW,
        "test_adapter",
        "1.0.0",
        "native_json",
        hashlib.sha256(artifact).hexdigest(),
        len(artifact),
        features,
        schema_fingerprint(features),
        labels,
        schema_fingerprint(labels),
        (source,),
        "iex",
        "1m",
        "raw",
        "research_universe_100_v1",
        (TimeRange(datetime(2025, 10, 9, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC)),),
        (TimeRange(datetime(2026, 8, 2, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC)),),
        "purged_walk_forward",
        {"entry_bps": "5", "exit_bps": "5"},
        271828,
        {"python": "3.12", "adapter": "test-1.0.0"},
        "c" * 64,
        {"mae": "0.01"},
    )


def test_manifest_round_trip_is_strict_and_rejects_pickle() -> None:
    manifest = _manifest(b'{"tree": []}')
    restored = ModelArtifactManifest.from_json(manifest.canonical())
    assert restored.canonical() == manifest.canonical()
    mapping = manifest.to_mapping()
    mapping["unknown"] = True
    import json

    with pytest.raises(ValueError, match="missing or unknown"):
        ModelArtifactManifest.from_json(json.dumps(mapping, default=str))
    with pytest.raises(ValueError, match="safe native"):
        replace(manifest, artifact_format="pickle")
    with pytest.raises(ValueError, match="reserved final holdout"):
        replace(
            manifest,
            validation_ranges=(
                TimeRange(
                    datetime(2026, 9, 21, tzinfo=UTC),
                    datetime(2026, 10, 9, tzinfo=UTC),
                ),
            ),
        )


def test_repository_is_atomic_immutable_and_checksum_verified(tmp_path: Path) -> None:
    artifact = b'{"tree": []}'
    repository = FileModelRepository(tmp_path)
    manifest = _manifest(artifact)
    stored = repository.put(manifest, artifact)
    assert stored.manifest_path.exists() and stored.artifact_path.exists()
    assert repository.get("pooled_intraday", "1.0.0") == (manifest, artifact)
    # Exact retries are idempotent; the same pinned version can never be replaced.
    repository.put(manifest, artifact)
    replacement = b'{"different": true}'
    with pytest.raises(FileExistsError, match="immutable"):
        repository.put(_manifest(replacement), replacement)
    assert repository.get("pooled_intraday", "1.0.0") == (manifest, artifact)

    stored.artifact_path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="size mismatch|checksum mismatch"):
        repository.get("pooled_intraday", "1.0.0")
