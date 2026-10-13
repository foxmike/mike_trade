"""Immutable operator approvals that gate a pinned model's use by execution mode.

Training never makes a model tradeable.  An operator records an approval for one exact
``model_id@version`` and artifact checksum at a stage — ``shadow``, ``paper`` or ``live`` — and the
runtime refuses to start a mode whose models lack an approval at least that high.  Stages form a
ladder: ``paper`` requires an earlier ``shadow`` approval and ``live`` an earlier ``paper`` one.
``paper`` and ``live`` approvals must cite an out-of-sample backtest report that actually contains
the approved artifact.  Records are create-only (never rewritten) and bind the artifact bytes, so
a different artifact under the same id/version can never inherit an approval.  Approving a model
does not start anything, change a configuration, or place an order.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mike_trade.modeling.artifacts import FileModelRepository, ModelArtifactManifest, _atomic_create
from mike_trade.modeling.contracts import identifier, semver

__all__ = [
    "STAGES",
    "ApprovalError",
    "ModelApproval",
    "ApprovalRepository",
    "approve_model",
    "required_stage_for_mode",
    "pin_parameters",
]

STAGES = ("shadow", "paper", "live")
_MODE_STAGE = {"SHADOW": "shadow", "PAPER": "paper", "LIVE": "live"}
#: Report ``sample_role`` values acceptable as paper/live evidence.
_EVIDENCE_ROLES = frozenset({"out_of_sample_development_test"})


class ApprovalError(ValueError):
    """An approval is missing, inconsistent, or not permitted."""


def required_stage_for_mode(mode: str) -> str | None:
    """Stage a runtime ``mode`` needs (``None`` for BACKTEST, which never trades)."""
    return _MODE_STAGE.get(mode.strip().upper())


@dataclass(frozen=True, slots=True)
class ModelApproval:
    model_id: str
    model_version: str
    stage: str
    artifact_sha256: str
    manifest_sha256: str
    approved_by: str
    reason: str
    approved_at: datetime
    evidence: tuple[Mapping[str, Any], ...] = ()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": "mike_trade_model_approval",
            "schema_version": 1,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "stage": self.stage,
            "artifact_sha256": self.artifact_sha256,
            "manifest_sha256": self.manifest_sha256,
            "approved_by": self.approved_by,
            "reason": self.reason,
            "approved_at": self.approved_at.astimezone(UTC).isoformat(),
            "evidence": [dict(item) for item in self.evidence],
        }


def _manifest_digest(manifest: ModelArtifactManifest) -> str:
    return hashlib.sha256(manifest.canonical().encode()).hexdigest()


class ApprovalRepository:
    """``<model_root>/approvals/<model_id>/<version>/<stage>.json`` (create-only)."""

    def __init__(self, model_root: str | Path) -> None:
        self.root = Path(model_root) / "approvals"

    def path(self, model_id: str, version: str, stage: str) -> Path:
        if stage not in STAGES:
            raise ApprovalError(f"stage must be one of {list(STAGES)}")
        return (
            self.root
            / identifier(model_id, "model_id")
            / semver(version, "model_version")
            / f"{stage}.json"
        )

    def get(self, model_id: str, version: str, stage: str) -> ModelApproval | None:
        path = self.path(model_id, version, stage)
        if not path.is_file():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        return ModelApproval(
            raw["model_id"],
            raw["model_version"],
            raw["stage"],
            raw["artifact_sha256"],
            raw["manifest_sha256"],
            raw["approved_by"],
            raw["reason"],
            datetime.fromisoformat(raw["approved_at"]),
            tuple(raw.get("evidence", ())),
        )

    def list(self) -> tuple[ModelApproval, ...]:
        found: list[ModelApproval] = []
        if self.root.is_dir():
            for path in sorted(self.root.glob("*/*/*.json")):
                item = self.get(path.parent.parent.name, path.parent.name, path.stem)
                if item is not None:
                    found.append(item)
        return tuple(found)

    def put(self, approval: ModelApproval) -> Path:
        path = self.path(approval.model_id, approval.model_version, approval.stage)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(approval.to_mapping(), sort_keys=True, indent=2) + "\n").encode()
        if path.exists():
            raise ApprovalError(
                f"{approval.model_id}@{approval.model_version} already has a {approval.stage} "
                "approval; approvals are immutable"
            )
        _atomic_create(path, payload)
        return path

    def highest_valid_stage(self, manifest: ModelArtifactManifest) -> str | None:
        """Highest stage whose record still binds this exact artifact and manifest."""
        best: str | None = None
        for stage in STAGES:
            record = self.get(manifest.model_id, manifest.model_version, stage)
            if (
                record is not None
                and record.artifact_sha256 == manifest.artifact_sha256
                and record.manifest_sha256 == _manifest_digest(manifest)
            ):
                best = stage
        return best

    def require(self, manifest: ModelArtifactManifest, mode: str) -> None:
        """Raise unless ``manifest`` is approved at (or above) the stage ``mode`` needs."""
        needed = required_stage_for_mode(mode)
        if needed is None:
            return
        highest = self.highest_valid_stage(manifest)
        if highest is None or STAGES.index(highest) < STAGES.index(needed):
            raise ApprovalError(
                f"model {manifest.model_id}@{manifest.model_version} has no valid "
                f"'{needed}' approval for this exact artifact (highest: {highest or 'none'}); "
                "an operator must run model-approve"
            )


def _evidence_entry(path: Path, manifest: ModelArtifactManifest) -> dict[str, Any]:
    data = path.read_bytes()
    try:
        report = json.loads(data)
    except ValueError as exc:
        raise ApprovalError(f"evidence {path} is not a JSON report") from exc
    if report.get("schema") != "mike_trade_model_backtest_report":
        raise ApprovalError(f"evidence {path} is not a model-backtest report")
    models = [
        item
        for item in report.get("models", ())
        if item.get("artifact_sha256") == manifest.artifact_sha256
    ]
    if not models:
        raise ApprovalError(f"evidence {path} does not contain this artifact")
    role = models[0].get("sample_role")
    if role not in _EVIDENCE_ROLES:
        raise ApprovalError(
            f"evidence {path} describes a {role!r} run; paper/live approval needs an "
            "out-of-sample development test"
        )
    if report.get("holdout_evaluated"):
        raise ApprovalError("evidence must not be a holdout run")
    return {
        "kind": "model_backtest_report",
        "path": str(path),
        "sha256": hashlib.sha256(data).hexdigest(),
        "campaign_id": report.get("campaign_id"),
        "sample_role": role,
    }


def approve_model(
    model_root: str | Path,
    model_id: str,
    version: str,
    stage: str,
    *,
    approved_by: str,
    reason: str,
    evidence: Sequence[str | Path] = (),
    now: datetime | None = None,
) -> tuple[Path, ModelApproval]:
    """Record an approval after verifying the artifact, ladder and evidence."""
    if stage not in STAGES:
        raise ApprovalError(f"stage must be one of {list(STAGES)}")
    if not approved_by.strip() or not reason.strip():
        raise ApprovalError("approved_by and reason are required")
    manifest, _ = FileModelRepository(model_root).get(model_id, version)  # checksum-verified
    repository = ApprovalRepository(model_root)
    if stage != "shadow":
        previous = STAGES[STAGES.index(stage) - 1]
        record = repository.get(model_id, version, previous)
        if record is None or record.artifact_sha256 != manifest.artifact_sha256:
            raise ApprovalError(f"a '{previous}' approval must precede '{stage}'")
        if not evidence:
            raise ApprovalError(f"{stage} approval requires --evidence (a model-backtest report)")
    entries = tuple(_evidence_entry(Path(item), manifest) for item in evidence)
    approval = ModelApproval(
        manifest.model_id,
        manifest.model_version,
        stage,
        manifest.artifact_sha256,
        _manifest_digest(manifest),
        approved_by.strip(),
        reason.strip(),
        (now or datetime.now(UTC)).astimezone(UTC),
        entries,
    )
    return repository.put(approval), approval


def pin_parameters(manifest: ModelArtifactManifest) -> dict[str, str]:
    """Strategy parameters that pin exactly this artifact."""
    return {
        "model_id": manifest.model_id,
        "model_version": manifest.model_version,
        "artifact_sha256": manifest.artifact_sha256,
    }
