"""Explicit registry for trusted model-library adapters."""

from __future__ import annotations

from typing import Protocol

from mike_trade.modeling.artifacts import ModelArtifactManifest
from mike_trade.modeling.contracts import ModelPredictor, ModelTrainer, identifier, semver


class ModelAdapterFactory(Protocol):
    adapter_id: str
    version: str
    artifact_formats: tuple[str, ...]

    def trainer(self) -> ModelTrainer: ...

    def predictor(
        self, artifact: bytes, manifest: ModelArtifactManifest | None = None
    ) -> ModelPredictor: ...


class ModelRegistryError(LookupError):
    pass


class ModelAdapterRegistry:
    """No discovery or dynamic imports: composition code registers trusted factories."""

    def __init__(self) -> None:
        self._factories: dict[tuple[str, str], ModelAdapterFactory] = {}

    def register(self, factory: ModelAdapterFactory) -> None:
        adapter_id = identifier(factory.adapter_id, "adapter_id")
        version = semver(factory.version, "adapter_version")
        if not factory.artifact_formats:
            raise ValueError("model adapter must declare at least one artifact format")
        key = (adapter_id, version)
        if key in self._factories:
            raise ModelRegistryError(f"duplicate model adapter registration: {key}")
        self._factories[key] = factory

    def get(self, adapter_id: str, version: str) -> ModelAdapterFactory:
        key = (identifier(adapter_id, "adapter_id"), semver(version, "adapter_version"))
        try:
            return self._factories[key]
        except KeyError:
            raise ModelRegistryError(f"model adapter not found: {key}") from None

    def versions(self, adapter_id: str) -> tuple[str, ...]:
        normalized = identifier(adapter_id, "adapter_id")
        return tuple(sorted(version for name, version in self._factories if name == normalized))
