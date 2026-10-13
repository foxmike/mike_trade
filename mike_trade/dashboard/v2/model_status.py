"""Read-only model status under /api/v2/models/.

The engine publishes a ``models`` component to ``service_health`` (configured pins, loaded or
unavailable, last prediction, last decision per strategy and symbol, inference errors).  This
router only reads it; it loads no model and exposes no control.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from mike_trade.persistence.readstore import ReadStore


class ModelStrategyStatus(BaseModel):
    strategy: str
    symbol: str
    role: str
    model: str
    pinned_sha256: str | None = None
    state: str


class ModelLoadStatus(BaseModel):
    model: str
    algorithm: str
    role: str
    feed: str
    artifact_sha256: str
    state: str
    predictions: int = 0
    failures: int = 0
    last_prediction: dict[str, Any] | None = None
    last_failure: dict[str, Any] | None = None


class ModelDecisionSummary(BaseModel):
    strategy: str
    symbol: str
    decision: str
    reason: str
    observation_end: str | None = None
    decided_at: str


class ModelStatusResponse(BaseModel):
    account_id: str
    as_of: datetime
    published: bool
    component_status: str | None = None
    heartbeat_at: datetime | None = None
    inference_errors: int = 0
    strategies: list[ModelStrategyStatus] = Field(default_factory=list)
    models: list[ModelLoadStatus] = Field(default_factory=list)
    decisions: list[ModelDecisionSummary] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    note: str = "Read-only: model status is published by the engine; nothing here can change it."


def create_model_status_v2_router(service: Any) -> APIRouter:
    router = APIRouter(prefix="/api/v2/models", tags=["models"])

    @router.get("/status", response_model=ModelStatusResponse)
    def model_status(account_id: str | None = Query(default=None)) -> ModelStatusResponse:
        store = ReadStore.open(service._db_path)
        try:
            scopes = store.list_account_scopes()
            scope = (account_id or "").strip() or (
                str(scopes[0]["account_scope_id"]) if scopes else "default"
            )
            row = store._view.execute(
                "SELECT status, heartbeat_at, details_json FROM service_health "
                "WHERE account_scope_id = ? AND component = 'models'",
                (scope,),
            ).fetchone()
        finally:
            store.close()
        now = datetime.now(UTC)
        if row is None:
            return ModelStatusResponse(account_id=scope, as_of=now, published=False)
        details = json.loads(row[2]) if row[2] else {}
        summary = json.loads(str(details.get("summary_json", "{}")))
        heartbeat = None
        if row[1]:
            heartbeat = datetime.fromisoformat(str(row[1]).replace("Z", "+00:00"))
        return ModelStatusResponse(
            account_id=scope,
            as_of=now,
            published=True,
            component_status=str(row[0]),
            heartbeat_at=heartbeat,
            inference_errors=int(summary.get("inference_errors", 0)),
            strategies=[ModelStrategyStatus(**item) for item in summary.get("strategies", [])],
            models=[ModelLoadStatus(**item) for item in summary.get("models", [])],
            decisions=[ModelDecisionSummary(**item) for item in summary.get("decisions", [])],
            diagnostics=dict(summary.get("diagnostics", {})),
        )

    return router
