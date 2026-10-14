"""Append-only review records plus the cache/replay index built from them.

Every review - live inference, cache reuse, or replay - becomes one :class:`ReviewRecord` with the
stable input hash, reviewer identity, request and response documents, explanation, usage/cost,
latency, errors, and what the engine did with it.  Records are stored as JSON Lines
(``*.reviews.jsonl``), one file per run in BACKTEST and one per mode in SHADOW/PAPER/LIVE.

Replay and caching share one lookup: ``find(input_hash)`` returns a previously produced *usable*
review (one with a structured decision) whose complete input hashed identically.  A recorded
failure is never replayed as a decision.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from mike_trade.domain.errors import MikeTradeError
from mike_trade.review.reviewer import ReviewDecision, canonical_json

__all__ = [
    "RECORD_SCHEMA_VERSION",
    "ReviewStoreError",
    "ReviewRecord",
    "ReviewStore",
    "MemoryReviewStore",
    "JsonlReviewStore",
    "load_review_records",
]

RECORD_SCHEMA_VERSION = 1
_SUFFIX = ".reviews.jsonl"


class ReviewStoreError(MikeTradeError):
    """The recorded reviews cannot be used (corrupt file, ambiguous identity)."""


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    """One persisted review.  ``request``/``response`` are the exact JSON documents exchanged."""

    review_id: str
    input_hash: str
    mode: str
    enforcement: str
    source: str  # live_inference | cache | replay
    reviewer: Mapping[str, Any]
    identity_key: str
    as_of: str
    proposal_id: str
    action: str
    symbol: str
    strategy_ids: tuple[str, ...]
    selected_strategy_id: str
    signal_id: str
    request: Mapping[str, Any]
    effect: str
    requested_at: str | None = None
    response: Mapping[str, Any] | None = None
    usage: Mapping[str, Any] | None = None
    latency_ms: float | None = None
    error: Mapping[str, Any] | None = None
    derived_from: str | None = None
    point_in_time_safe: bool = False
    schema_version: int = field(default=RECORD_SCHEMA_VERSION)

    @property
    def decision(self) -> ReviewDecision | None:
        if self.response is None:
            return None
        return ReviewDecision(self.response["decision"])

    @property
    def explanation(self) -> str | None:
        return None if self.response is None else str(self.response["reason"])

    @property
    def usable(self) -> bool:
        return (
            self.response is not None and self.error is None and self.source != "disposition"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "review_id": self.review_id,
            "input_hash": self.input_hash,
            "mode": self.mode,
            "enforcement": self.enforcement,
            "source": self.source,
            "derived_from": self.derived_from,
            "reviewer": dict(self.reviewer),
            "identity_key": self.identity_key,
            "point_in_time_safe": self.point_in_time_safe,
            "requested_at": self.requested_at,
            "as_of": self.as_of,
            "proposal_id": self.proposal_id,
            "action": self.action,
            "symbol": self.symbol,
            "strategy_ids": list(self.strategy_ids),
            "selected_strategy_id": self.selected_strategy_id,
            "signal_id": self.signal_id,
            "request": dict(self.request),
            "response": None if self.response is None else dict(self.response),
            "decision": None if self.response is None else self.response["decision"],
            "explanation": self.explanation,
            "usage": None if self.usage is None else dict(self.usage),
            "latency_ms": self.latency_ms,
            "error": None if self.error is None else dict(self.error),
            "effect": self.effect,
        }

    def summary(self) -> dict[str, Any]:
        """The deterministic part (no wall clock, latency, usage) embedded in run artifacts.

        Replaying a recorded run reproduces this exactly, so a replayed result hashes the same as
        the original even though the original carried latency and cost.
        """
        return {
            "review_id": self.review_id,
            "input_hash": self.input_hash,
            "as_of": self.as_of,
            "proposal_id": self.proposal_id,
            "action": self.action,
            "symbol": self.symbol,
            "strategy_ids": list(self.strategy_ids),
            "selected_strategy_id": self.selected_strategy_id,
            "signal_id": self.signal_id,
            "enforcement": self.enforcement,
            "reviewer": dict(self.reviewer),
            "decision": None if self.response is None else self.response["decision"],
            "explanation": self.explanation,
            "observations": [] if self.response is None else list(self.response["observations"]),
            "reviewer_version": None if self.response is None
            else self.response["reviewer_version"],
            "error": None if self.error is None else self.error.get("type"),
            "effect": self.effect,
            "point_in_time_safe": self.point_in_time_safe,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReviewRecord:
        try:
            if int(data.get("schema_version", 0)) != RECORD_SCHEMA_VERSION:
                raise ValueError(f"unsupported record schema_version {data.get('schema_version')}")
            return cls(
                review_id=data["review_id"],
                input_hash=data["input_hash"],
                mode=data["mode"],
                enforcement=data["enforcement"],
                source=data["source"],
                derived_from=data.get("derived_from"),
                reviewer=data["reviewer"],
                identity_key=data["identity_key"],
                point_in_time_safe=bool(data.get("point_in_time_safe", False)),
                requested_at=data.get("requested_at"),
                as_of=data["as_of"],
                proposal_id=data["proposal_id"],
                action=data["action"],
                symbol=data["symbol"],
                strategy_ids=tuple(data["strategy_ids"]),
                selected_strategy_id=data["selected_strategy_id"],
                signal_id=data["signal_id"],
                request=data["request"],
                response=data.get("response"),
                usage=data.get("usage"),
                latency_ms=data.get("latency_ms"),
                error=data.get("error"),
                effect=data["effect"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ReviewStoreError(f"malformed review record: {exc}") from exc


class ReviewStore(Protocol):
    def find(
        self, input_hash: str, *, identity_key: str | None = None,
        expect: Mapping[str, str] | None = None,
    ) -> ReviewRecord | None: ...

    def append(self, record: ReviewRecord) -> None: ...


def _matches(record: ReviewRecord, expect: Mapping[str, str] | None) -> bool:
    if not expect:
        return True
    reviewer = record.reviewer
    for key in ("provider", "reviewer", "model"):
        wanted = expect.get(key)
        if wanted is not None and reviewer.get(key) != wanted:
            return False
    version = expect.get("version")
    return version is None or (
        record.response is not None and record.response.get("reviewer_version") == version
    )


class _Index:
    """input_hash -> usable records, oldest first (the original is preferred over its copies)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_hash: dict[str, list[ReviewRecord]] = {}

    def add(self, record: ReviewRecord) -> None:
        if not record.usable:
            return
        with self._lock:
            self._by_hash.setdefault(record.input_hash, []).append(record)

    def find(
        self, input_hash: str, identity_key: str | None, expect: Mapping[str, str] | None
    ) -> ReviewRecord | None:
        with self._lock:
            candidates = list(self._by_hash.get(input_hash, ()))
        candidates = [
            record for record in candidates
            if (identity_key is None or record.identity_key == identity_key)
            and _matches(record, expect)
        ]
        if not candidates:
            return None
        identities = {record.identity_key for record in candidates}
        if len(identities) > 1:
            raise ReviewStoreError(
                f"input {input_hash[:12]} has recorded reviews from {len(identities)} different "
                "reviewer identities; name the one to replay with decision_review.expect_reviewer"
            )
        originals = [record for record in candidates if record.source == "live_inference"]
        return (originals or candidates)[0]


class MemoryReviewStore:
    """In-process store (tests, and the in-run cache layer)."""

    def __init__(self, records: Iterable[ReviewRecord] = ()) -> None:
        self._index = _Index()
        self.records: list[ReviewRecord] = []
        for record in records:
            self.append(record)

    def find(
        self, input_hash: str, *, identity_key: str | None = None,
        expect: Mapping[str, str] | None = None,
    ) -> ReviewRecord | None:
        return self._index.find(input_hash, identity_key, expect)

    def append(self, record: ReviewRecord) -> None:
        self.records.append(record)
        self._index.add(record)


def _iter_files(paths: Sequence[str | Path]) -> list[Path]:
    files: list[Path] = []
    for item in paths:
        path = Path(item)
        if path.is_dir():
            files.extend(sorted(path.rglob(f"*{_SUFFIX}")))
        elif path.is_file():
            files.append(path)
        else:
            raise ReviewStoreError(f"recorded reviews not found: {path}")
    return files


def load_review_records(paths: Sequence[str | Path]) -> list[ReviewRecord]:
    """Read every record from files or directories of ``*.reviews.jsonl``."""
    records: list[ReviewRecord] = []
    for file in _iter_files(paths):
        with file.open(encoding="utf-8") as stream:
            for number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    records.append(ReviewRecord.from_dict(json.loads(line)))
                except (json.JSONDecodeError, ReviewStoreError) as exc:
                    raise ReviewStoreError(f"{file}:{number}: {exc}") from exc
    return records


class JsonlReviewStore:
    """JSONL-backed store: append to ``path`` (when given) and index ``read_paths`` + ``path``.

    Appends take an exclusive ``flock`` so several worker processes can share one cache file; a
    record appended by another process becomes visible to this one after :meth:`refresh`.
    """

    def __init__(
        self, path: str | Path | None = None, *, read_paths: Sequence[str | Path] = ()
    ) -> None:
        self.path = None if path is None else Path(path)
        self._index = _Index()
        sources: list[str | Path] = list(read_paths)
        if self.path is not None and self.path.exists():
            sources.append(self.path)
        for record in load_review_records(sources):
            self._index.add(record)

    def refresh(self) -> None:
        if self.path is not None and self.path.exists():
            fresh = _Index()
            for record in load_review_records([self.path]):
                fresh.add(record)
            self._index = fresh

    def find(
        self, input_hash: str, *, identity_key: str | None = None,
        expect: Mapping[str, str] | None = None,
    ) -> ReviewRecord | None:
        return self._index.find(input_hash, identity_key, expect)

    def append(self, record: ReviewRecord) -> None:
        self._index.add(record)
        if self.path is None:
            return
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = canonical_json(record.to_dict()) + "\n"
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                stream.write(line)
                stream.flush()
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
