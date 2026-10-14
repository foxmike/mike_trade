"""Provider-independent trade-review contract: decisions, strict response parsing, errors.

A :class:`TradeDecisionReviewer` receives one compact, JSON-serializable review request and
returns a :class:`ReviewResponse` (``APPROVE`` or ``REJECT`` plus an explanation).  It never
places orders, never receives credentials or unrelated account data, and its answer is accepted
only in the structured form below - free text is never interpreted as an approval.

Concrete providers (:mod:`mike_trade.review.openrouter` is the first) implement the protocol; the
stage in :mod:`mike_trade.review.stage` is the only caller.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from mike_trade.domain.errors import MikeTradeError

__all__ = [
    "REVIEW_PROTOCOL",
    "REVIEW_RESPONSE_SCHEMA",
    "ReviewDecision",
    "ReviewerIdentity",
    "ReviewUsage",
    "ReviewResponse",
    "ReviewError",
    "ReviewerTimeout",
    "ReviewerUnavailable",
    "MalformedReviewResponse",
    "TradeDecisionReviewer",
    "parse_review_payload",
    "canonical_json",
]

#: Version of the request/response contract between the engine and any reviewer provider.
REVIEW_PROTOCOL = "mike_trade.trade_review/1"

#: The only response shape a reviewer may produce (providers hand this to the model).
REVIEW_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "reason", "observations"],
    "properties": {
        "decision": {"type": "string", "enum": ["APPROVE", "REJECT"]},
        "reason": {"type": "string", "minLength": 1},
        "observations": {"type": "array", "items": {"type": "string"}},
    },
}


def canonical_json(value: object) -> str:
    """Deterministic JSON used for every hash in the review subsystem."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class ReviewDecision(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class ReviewError(MikeTradeError):
    """Base class for every reviewer failure handled by the configured error policy."""


class ReviewerTimeout(ReviewError):
    """The reviewer did not answer within the configured timeout."""


class ReviewerUnavailable(ReviewError):
    """The provider could not be reached or answered with an error status."""


class MalformedReviewResponse(ReviewError):
    """The reviewer answered, but not with a valid structured decision."""


@dataclass(frozen=True, slots=True)
class ReviewerIdentity:
    """Who produced a review, precisely enough to decide whether a recorded one may be reused.

    ``settings`` holds only non-secret, outcome-relevant knobs (temperature, prompt version,
    response format).  ``point_in_time_safe`` is the operator's attestation that the reviewer has
    no access to information after the review time (no live news, no web search, no knowledge of
    the future); it is ``False`` unless explicitly attested, and historical results produced by a
    reviewer that is not attested are never presented as point-in-time valid.
    """

    provider: str
    reviewer: str
    model: str
    protocol: str = REVIEW_PROTOCOL
    settings: Mapping[str, Any] = field(default_factory=dict)
    point_in_time_safe: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "reviewer": self.reviewer,
            "model": self.model,
            "protocol": self.protocol,
            "settings": dict(self.settings),
            "point_in_time_safe": self.point_in_time_safe,
        }

    def matching_fields(self) -> dict[str, Any]:
        """The fields that decide cache/replay equivalence (``point_in_time_safe`` excluded)."""
        data = self.to_dict()
        data.pop("point_in_time_safe")
        return data

    @property
    def key(self) -> str:
        return hashlib.sha256(canonical_json(self.matching_fields()).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ReviewUsage:
    """Provider-reported usage and cost for one request (``None`` when not reported)."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cost: Decimal | None = None
    cost_currency: str = "USD"
    served_model: str | None = None
    generation_id: str | None = None
    served_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost": None if self.cost is None else str(self.cost),
            "cost_currency": self.cost_currency,
            "served_model": self.served_model,
            "generation_id": self.generation_id,
            "served_by": self.served_by,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> ReviewUsage | None:
        if not data:
            return None
        try:
            cost = data.get("cost")
            return cls(
                input_tokens=data.get("input_tokens"),
                output_tokens=data.get("output_tokens"),
                total_tokens=data.get("total_tokens"),
                cost=None if cost is None else Decimal(str(cost)),
                cost_currency=str(data.get("cost_currency", "USD")),
                served_model=data.get("served_model"),
                generation_id=data.get("generation_id"),
                served_by=data.get("served_by"),
            )
        except (InvalidOperation, ValueError) as exc:
            raise MalformedReviewResponse(f"malformed recorded usage: {exc}") from exc


@dataclass(frozen=True, slots=True)
class ReviewResponse:
    """A validated reviewer answer.  ``reviewer_version`` is stamped by the provider adapter."""

    decision: ReviewDecision
    reason: str
    observations: tuple[str, ...]
    reviewer_version: str
    usage: ReviewUsage | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.decision, ReviewDecision):
            raise MalformedReviewResponse("decision must be a ReviewDecision")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise MalformedReviewResponse("reason must be a non-empty string")
        if not isinstance(self.reviewer_version, str) or not self.reviewer_version.strip():
            raise MalformedReviewResponse("reviewer_version must be a non-empty string")

    def to_dict(self) -> dict[str, Any]:
        """The persisted structured decision (usage is stored separately on the record)."""
        return {
            "decision": self.decision.value,
            "reason": self.reason,
            "observations": list(self.observations),
            "reviewer_version": self.reviewer_version,
        }


def parse_review_payload(
    payload: object, *, reviewer_version: str, usage: ReviewUsage | None = None
) -> ReviewResponse:
    """Strictly validate a decoded JSON answer; anything else is :class:`MalformedReviewResponse`.

    Accepts exactly ``{"decision": "APPROVE"|"REJECT", "reason": str, "observations": [str]}``:
    case-sensitive decision, no extra keys, no coercion.  Never searches free text for a verdict.
    """
    if not isinstance(payload, Mapping):
        raise MalformedReviewResponse(
            f"a review must be a JSON object; got {type(payload).__name__}"
        )
    unexpected = sorted(set(payload) - {"decision", "reason", "observations"})
    if unexpected:
        raise MalformedReviewResponse(f"unexpected review fields: {unexpected}")
    missing = [key for key in ("decision", "reason", "observations") if key not in payload]
    if missing:
        raise MalformedReviewResponse(f"missing review fields: {missing}")
    raw_decision = payload["decision"]
    if not isinstance(raw_decision, str) or raw_decision not in ReviewDecision.__members__:
        raise MalformedReviewResponse(
            f"decision must be exactly 'APPROVE' or 'REJECT'; got {raw_decision!r}"
        )
    reason = payload["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise MalformedReviewResponse("reason must be a non-empty string")
    observations = payload["observations"]
    if not isinstance(observations, list) or any(not isinstance(i, str) for i in observations):
        raise MalformedReviewResponse("observations must be a list of strings")
    return ReviewResponse(
        decision=ReviewDecision(raw_decision),
        reason=reason.strip(),
        observations=tuple(observations),
        reviewer_version=reviewer_version,
        usage=usage,
    )


@runtime_checkable
class TradeDecisionReviewer(Protocol):
    """A provider-independent reviewer.  Implementations must be safe to call concurrently."""

    @property
    def identity(self) -> ReviewerIdentity: ...

    async def review(self, request: Mapping[str, Any]) -> ReviewResponse:
        """Review one compact request.  Raise a :class:`ReviewError` subclass on failure."""
        ...

    async def aclose(self) -> None: ...
