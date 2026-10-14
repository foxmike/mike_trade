"""The universal review stage that sits between strategy arbitration and order submission.

Flow (identical in BACKTEST, SHADOW, PAPER and LIVE)::

    strategies signal -> existing arbitration selects an action -> TradeProposal + evidence
        -> DecisionReviewStage.review()  -> APPROVE / REJECT
        -> (enforce) the ordinary order path runs only when allowed

What the stage guarantees:

* **Scope.**  Only ``BUY`` and strategy ``SELL`` proposals whose action, symbol and selected
  strategy are enabled are reviewed.  Protective stops, end-of-day and kill-switch liquidations,
  manual and hard-risk exits never reach it (a proposal for any exit source other than
  ``STRATEGY_SELL`` raises :class:`MandatoryExitReviewError`).
* **Modes.**  ``enforce``: APPROVE allows, REJECT blocks the ordinary action.  ``advisory``: the
  opinion is recorded and execution is never altered (live modes do not even wait for it).
* **Errors.**  Timeout, transport failure and malformed responses follow ``on_error``:
  ``bypass`` lets the ordinary action proceed, ``reject`` blocks it (enforce mode only).
* **Cost control.**  An identical complete input is answered from the cache / recorded reviews
  instead of a second paid call, and concurrent identical requests share one call.  Replay
  (``source: replay``) never calls a model; a missing recording is an error, not a bypass.
* **Validity.**  A review that returns after the market, position or protective-order state
  changed cannot authorize the proposal (:meth:`DecisionReviewStage.authorize`).
* **Recording.**  Every review is persisted as a :class:`~mike_trade.review.store.ReviewRecord`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from mike_trade.domain.errors import ConfigurationError, MikeTradeError
from mike_trade.review.config import DecisionReviewConfig
from mike_trade.review.evidence import EvidenceError, require_evidence_support
from mike_trade.review.proposal import (
    StateToken,
    TradeProposal,
    build_review_request,
    request_hash,
)
from mike_trade.review.reviewer import (
    MalformedReviewResponse,
    ReviewDecision,
    ReviewerIdentity,
    ReviewerTimeout,
    ReviewError,
    ReviewResponse,
    TradeDecisionReviewer,
)
from mike_trade.review.store import ReviewRecord, ReviewStore

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "ReviewReplayMiss",
    "MandatoryExitReviewError",
    "ReviewOutcome",
    "DecisionReviewStage",
    "protective_exit",
    "PROTECTIVE_EXIT_ATTRIBUTE",
]

LOGGER = logging.getLogger("mike_trade.review")

#: A strategy class attribute marking its signals as mandatory protective exits (never reviewed).
PROTECTIVE_EXIT_ATTRIBUTE = "PROTECTIVE_EXIT"

# Effects (what the stage decided for the ordinary action).
NOT_REVIEWED = "NOT_REVIEWED"
APPROVED = "APPROVED"
REJECTED = "REJECTED_BLOCKED"
ADVISORY_APPROVE = "ADVISORY_APPROVE"
ADVISORY_REJECT = "ADVISORY_REJECT_NOT_ENFORCED"
ERROR_BYPASSED = "ERROR_BYPASSED"
ERROR_REJECTED = "ERROR_REJECTED_BLOCKED"
ADVISORY_ERROR = "ADVISORY_ERROR"
STALE_BLOCKED = "STALE_BLOCKED"


class ReviewReplayMiss(MikeTradeError):
    """Replay mode found no recorded review for a proposal; the run cannot be reproduced."""


class MandatoryExitReviewError(MikeTradeError):
    """Something tried to put a mandatory protective/emergency exit through review."""


def protective_exit(strategy: object) -> bool:
    """Whether ``strategy`` only ever raises mandatory protective exits (never reviewable)."""
    return bool(getattr(strategy, PROTECTIVE_EXIT_ATTRIBUTE, False))


@dataclass(frozen=True, slots=True)
class ReviewOutcome:
    """What the integration must do.  ``allow`` is the only field execution may act on."""

    proposal_id: str
    reviewed: bool
    allow: bool
    effect: str
    decision: ReviewDecision | None = None
    record: ReviewRecord | None = None
    reason: str = ""
    stale_fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Call:
    response: ReviewResponse | None
    error: Mapping[str, str] | None
    latency_ms: float
    requested_at: str


class _RateLimiter:
    """Spaces requests to at most ``per_minute``; ``None`` disables limiting (the default)."""

    def __init__(self, per_minute: float | None) -> None:
        self._interval = None if not per_minute else 60.0 / per_minute
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def wait(self) -> None:
        if self._interval is None:
            return
        async with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next - now)
            self._next = max(now, self._next) + self._interval
        if delay:
            await asyncio.sleep(delay)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class DecisionReviewStage:
    """Orchestrates scope, caching/replay, the reviewer call, error policy and recording."""

    def __init__(
        self,
        config: DecisionReviewConfig,
        *,
        mode: str,
        reviewer: TradeDecisionReviewer | None = None,
        record_store: ReviewStore | None = None,
        cache_store: ReviewStore | None = None,
        replay_store: ReviewStore | None = None,
        max_concurrent: int = 16,
        requests_per_minute: float | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if config.source == "replay":
            if replay_store is None:
                raise ConfigurationError(
                    "source: replay needs recorded reviews", code="DECISION_REVIEW_INVALID"
                )
            if reviewer is not None:
                raise ConfigurationError(
                    "replay must not be given a live reviewer", code="DECISION_REVIEW_INVALID"
                )
        elif config.active and reviewer is None:
            raise ConfigurationError(
                "decision_review is active with source: live but no reviewer is configured",
                code="DECISION_REVIEW_INVALID",
            )
        if max_concurrent < 1:
            raise ConfigurationError("max_concurrent must be >= 1", code="DECISION_REVIEW_INVALID")
        self.config = config
        self.mode = mode
        self.reviewer = reviewer
        self._record_store = record_store
        self._cache = cache_store if config.cache_enabled else None
        self._replay = replay_store
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._limiter = _RateLimiter(requests_per_minute)
        self._now = now
        self._inflight: dict[str, asyncio.Future[_Call]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        #: Every record this stage produced, in order (the run's review ledger).
        self.records: list[ReviewRecord] = []

    # ---------------------------------------------------------------- configuration

    @property
    def active(self) -> bool:
        return self.config.active

    @property
    def enforcing(self) -> bool:
        return self.config.enforcing

    def reviews_action(self, action: str) -> bool:
        return self.config.reviews_action(action)

    def in_scope(self, action: str, symbol: str, strategy_id: str) -> bool:
        """Whether a proposal of ``action`` on ``symbol`` selected from ``strategy_id`` is reviewed."""
        config = self.config
        return (
            config.reviews_action(action)
            and (not config.symbols or symbol.upper() in config.symbols)
            and (not config.strategies or strategy_id in config.strategies)
        )

    def validate_strategies(
        self, configured: Iterable[tuple[str, str, object]]
    ) -> None:
        """Fail closed at startup for any in-scope strategy that cannot produce evidence.

        ``configured`` yields ``(symbol, action, strategy_class_or_instance)`` for every
        configured BUY/SELL strategy.  Strategies named in ``decision_review.strategies`` must
        exist; mandatory protective exits may not be named at all.
        """
        seen_ids: set[str] = set()
        for symbol, action, strategy in configured:
            spec = getattr(strategy, "spec", None)
            strategy_id = getattr(spec, "strategy_id", repr(strategy))
            seen_ids.add(strategy_id)
            if action not in ("BUY", "SELL"):
                continue
            if protective_exit(strategy):
                if strategy_id in self.config.strategies:
                    raise ConfigurationError(
                        f"strategy {strategy_id!r} raises mandatory protective exits and can "
                        "never be reviewed; remove it from decision_review.strategies",
                        code="DECISION_REVIEW_PROTECTIVE_STRATEGY",
                        context={"strategy_id": strategy_id},
                    )
                continue
            if self.in_scope(action, symbol, strategy_id):
                require_evidence_support(strategy, where=f"{symbol} {action}")
        unknown = sorted(set(self.config.strategies) - seen_ids)
        if self.config.strategies and unknown:
            raise ConfigurationError(
                f"decision_review.strategies names strategies that are not configured: {unknown}",
                code="DECISION_REVIEW_UNKNOWN_STRATEGY",
                context={"strategies": unknown},
            )

    def evidence_wanted(self, action: str, symbol: str, strategy_id: str) -> bool:
        """Whether evidence must be built for a signal (in scope and not a protective exit)."""
        return self.active and self.in_scope(action, symbol, strategy_id)

    # ---------------------------------------------------------------- validity

    def still_valid(
        self, proposal: TradeProposal, current: StateToken, now: datetime
    ) -> tuple[bool, tuple[str, ...]]:
        """May a review obtained for ``proposal`` still authorize it given ``current`` state?

        Structural facts (flat/cycle/quantity/pending orders/protective-order status/entry gate)
        must be unchanged.  If a newer completed bar exists the proposal also expires once it is
        older than ``max_proposal_age_seconds``.
        """
        original = proposal.state
        if original is None:
            return True, ()
        changed = list(original.changed_fields(current))
        if (
            current.latest_bar_end != original.latest_bar_end
            and (now - proposal.as_of).total_seconds() > self.config.max_proposal_age_seconds
        ):
            changed.append("market_moved_on")
        return not changed, tuple(changed)

    def authorize(
        self, outcome: ReviewOutcome, proposal: TradeProposal, current: StateToken, now: datetime
    ) -> ReviewOutcome:
        """Re-check validity after the review returned; an invalid proposal is never allowed.

        Call this under the same serialization as the order path, immediately before submitting.
        Only an *enforcing* allow can be withdrawn: advisory reviews never alter execution (the
        ordinary path performs its own checks), and an outcome that already blocks stays blocked.
        """
        if not outcome.reviewed or not outcome.allow or not self.enforcing:
            return outcome
        valid, changed = self.still_valid(proposal, current, now)
        if valid:
            return outcome
        record = outcome.record
        if record is not None:
            disposition = replace(
                record, source="disposition", effect=STALE_BLOCKED, usage=None, latency_ms=None,
                derived_from=record.review_id,
            )
            self._persist(disposition)
        return replace(
            outcome, allow=False, effect=STALE_BLOCKED, stale_fields=changed,
            reason=f"proposal no longer valid: {', '.join(changed)}",
        )

    # ---------------------------------------------------------------- review

    async def review(self, proposal: TradeProposal, context: MarketContext) -> ReviewOutcome:
        """Obtain and apply a review for ``proposal``.  Never raises for reviewer failures."""
        if proposal.exit_source not in (None, "STRATEGY_SELL"):
            raise MandatoryExitReviewError(
                f"{proposal.symbol}: {proposal.exit_source} exits are mandatory and are never "
                "reviewed"
            )
        if proposal.action == "SELL" and proposal.exit_source is None:
            raise MandatoryExitReviewError("a SELL proposal must declare exit_source")
        if not self.in_scope(proposal.action, proposal.symbol, proposal.selected_strategy_id):
            return ReviewOutcome(proposal.proposal_id, False, True, NOT_REVIEWED)
        selected = next(item for item in proposal.contributors if item.selected)
        if selected.evidence is None:  # evidence is mandatory for every reviewed strategy
            return self.fail(
                proposal,
                EvidenceError(f"{selected.strategy_id}: no evidence was produced for the signal"),
            )
        try:
            request = build_review_request(
                proposal, context, candle_count=self.config.candle_count,
                daily_candle_count=self.config.daily_candle_count,
            )
        except MikeTradeError:
            raise  # a look-ahead leak is a defect: never turned into a bypass
        except Exception as exc:  # noqa: BLE001 - request construction failure follows policy
            return self._apply_error(proposal, {}, "", exc)
        digest = request_hash(request)

        replay = self.config.source == "replay"
        identity = None if self.reviewer is None else self.reviewer.identity
        source_store = self._replay if replay else self._cache
        hit = None
        if source_store is not None:
            expected = (self.config.expect_reviewer or None) if replay else None
            hit = source_store.find(
                digest,
                identity_key=None if replay or identity is None else identity.key,
                expect=expected,
            )
        if hit is None and replay:
            raise ReviewReplayMiss(
                f"{proposal.proposal_id}: no recorded review for input {digest[:12]} "
                f"(replay_from={list(self.config.replay_from)})"
            )
        if hit is not None:
            return self._from_recorded(proposal, request, digest, hit, replay)

        assert self.reviewer is not None and identity is not None
        call, shared = await self._call_shared(digest, request)
        return self._from_call(proposal, request, digest, identity, call, shared)

    def fail(self, proposal: TradeProposal, exc: BaseException) -> ReviewOutcome:
        """Apply the error policy when the proposal could not even be assembled (no evidence)."""
        if not self.in_scope(proposal.action, proposal.symbol, proposal.selected_strategy_id):
            return ReviewOutcome(proposal.proposal_id, False, True, NOT_REVIEWED)
        identity = None if self.reviewer is None else self.reviewer.identity
        return self._apply_error(proposal, {}, "", exc, identity)

    def review_nonblocking(
        self, proposal: TradeProposal, context: MarketContext
    ) -> asyncio.Task[ReviewOutcome | None]:
        """Advisory reviews: record the opinion in the background; execution never waits."""

        async def run() -> ReviewOutcome | None:
            try:
                return await self.review(proposal, context)
            except Exception:  # noqa: BLE001 - an advisory failure must never reach trading
                LOGGER.exception(
                    "advisory review failed", extra={"proposal": proposal.proposal_id}
                )
                return None

        task = asyncio.get_running_loop().create_task(run(), name=f"review-{proposal.proposal_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def drain(self) -> None:
        """Wait for background advisory reviews (call on shutdown)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def aclose(self) -> None:
        await self.drain()
        if self.reviewer is not None:
            await self.reviewer.aclose()

    # ---------------------------------------------------------------- internals

    async def _call_shared(self, digest: str, request: Mapping[str, Any]) -> tuple[_Call, bool]:
        """One reviewer call per distinct input at a time; identical concurrent callers share it."""
        pending = self._inflight.get(digest)
        if pending is not None:
            return await asyncio.shield(pending), True
        future: asyncio.Future[_Call] = asyncio.get_running_loop().create_future()
        self._inflight[digest] = future
        try:
            call = await self._call(request)
            future.set_result(call)
            return call, False
        finally:
            self._inflight.pop(digest, None)
            if not future.done():
                future.cancel()

    async def _call(self, request: Mapping[str, Any]) -> _Call:
        assert self.reviewer is not None
        requested_at = _utc_iso(self._now())
        started = time.perf_counter()
        response: ReviewResponse | None = None
        error: dict[str, str] | None = None
        try:
            async with self._semaphore:
                await self._limiter.wait()
                response = await asyncio.wait_for(
                    self.reviewer.review(request), timeout=self.config.timeout_seconds
                )
            if not isinstance(response, ReviewResponse):
                raise MalformedReviewResponse(
                    f"reviewer returned {type(response).__name__}, not a ReviewResponse"
                )
        except asyncio.TimeoutError:
            response, error = None, {
                "type": ReviewerTimeout.__name__,
                "message": f"no answer within {self.config.timeout_seconds}s",
            }
        except ReviewError as exc:
            response, error = None, {"type": type(exc).__name__, "message": str(exc)[:500]}
        except Exception as exc:  # noqa: BLE001 - any provider defect is a reviewer error
            response, error = None, {"type": type(exc).__name__, "message": str(exc)[:500]}
        latency = (time.perf_counter() - started) * 1000.0
        return _Call(response, error, round(latency, 3), requested_at)

    # -- record construction

    def _record(
        self,
        proposal: TradeProposal,
        request: Mapping[str, Any],
        digest: str,
        identity: Mapping[str, Any],
        identity_key: str,
        point_in_time_safe: bool,
        *,
        source: str,
        effect: str,
        response: Mapping[str, Any] | None,
        usage: Mapping[str, Any] | None,
        latency_ms: float | None,
        error: Mapping[str, Any] | None,
        requested_at: str | None,
        derived_from: str | None,
    ) -> ReviewRecord:
        review_id = hashlib.sha256(
            f"{digest}:{proposal.proposal_id}:{identity_key}".encode()
        ).hexdigest()[:32]
        return ReviewRecord(
            review_id=review_id,
            input_hash=digest,
            mode=self.mode,
            enforcement="enforce" if self.enforcing else "advisory",
            source=source,
            reviewer=identity,
            identity_key=identity_key,
            point_in_time_safe=point_in_time_safe,
            requested_at=requested_at,
            as_of=_utc_iso(proposal.as_of),
            proposal_id=proposal.proposal_id,
            action=proposal.action,
            symbol=proposal.symbol,
            strategy_ids=proposal.strategy_ids,
            selected_strategy_id=proposal.selected_strategy_id,
            signal_id=proposal.selected_signal_id,
            request=request,
            effect=effect,
            response=response,
            usage=usage,
            latency_ms=latency_ms,
            error=error,
            derived_from=derived_from,
        )

    def _persist(self, record: ReviewRecord) -> None:
        self.records.append(record)
        if self.config.record_reviews and self._record_store is not None:
            self._record_store.append(record)
        if self._cache is not None and record.source == "live_inference" and record.usable:
            self._cache.append(record)

    def _decide(self, decision: ReviewDecision) -> tuple[bool, str]:
        if self.enforcing:
            return (True, APPROVED) if decision is ReviewDecision.APPROVE else (False, REJECTED)
        return True, ADVISORY_APPROVE if decision is ReviewDecision.APPROVE else ADVISORY_REJECT

    def _from_recorded(
        self,
        proposal: TradeProposal,
        request: Mapping[str, Any],
        digest: str,
        hit: ReviewRecord,
        replay: bool,
    ) -> ReviewOutcome:
        assert hit.response is not None
        decision = ReviewDecision(hit.response["decision"])
        allow, effect = self._decide(decision)
        record = self._record(
            proposal, request, digest, hit.reviewer, hit.identity_key, hit.point_in_time_safe,
            source="replay" if replay else "cache", effect=effect, response=hit.response,
            usage=None, latency_ms=None, error=None, requested_at=hit.requested_at,
            derived_from=hit.review_id,
        )
        self._persist(record)
        return ReviewOutcome(
            proposal.proposal_id, True, allow, effect, decision, record, hit.response["reason"]
        )

    def _from_call(
        self,
        proposal: TradeProposal,
        request: Mapping[str, Any],
        digest: str,
        identity: ReviewerIdentity,
        call: _Call,
        shared: bool,
    ) -> ReviewOutcome:
        if call.response is None:
            return self._apply_error(proposal, request, digest, None, identity, call)
        decision = call.response.decision
        allow, effect = self._decide(decision)
        usage = call.response.usage
        record = self._record(
            proposal, request, digest, identity.to_dict(), identity.key,
            identity.point_in_time_safe,
            source="cache" if shared else "live_inference", effect=effect,
            response=call.response.to_dict(),
            usage=None if shared or usage is None else usage.to_dict(),
            latency_ms=None if shared else call.latency_ms, error=None,
            requested_at=call.requested_at, derived_from=None,
        )
        self._persist(record)
        return ReviewOutcome(
            proposal.proposal_id, True, allow, effect, decision, record, call.response.reason
        )

    def _apply_error(
        self,
        proposal: TradeProposal,
        request: Mapping[str, Any],
        digest: str,
        exc: BaseException | None = None,
        identity: ReviewerIdentity | None = None,
        call: _Call | None = None,
    ) -> ReviewOutcome:
        error = (
            call.error if call is not None and call.error is not None
            else {"type": type(exc).__name__, "message": str(exc)[:500]}
        )
        if not self.enforcing:
            allow, effect = True, ADVISORY_ERROR
        elif self.config.on_error == "bypass":
            allow, effect = True, ERROR_BYPASSED
        else:
            allow, effect = False, ERROR_REJECTED
        identity_dict = (
            identity.to_dict() if identity is not None
            else {"provider": None, "reviewer": self.config.reviewer, "model": self.config.model}
        )
        record = self._record(
            proposal, request, digest, identity_dict,
            "" if identity is None else identity.key,
            False if identity is None else identity.point_in_time_safe,
            source="live_inference", effect=effect, response=None, usage=None,
            latency_ms=None if call is None else call.latency_ms, error=error,
            requested_at=None if call is None else call.requested_at, derived_from=None,
        )
        self._persist(record)
        LOGGER.warning(
            "decision review error", extra={"proposal": proposal.proposal_id, "error": dict(error)}
        )
        return ReviewOutcome(
            proposal.proposal_id, True, allow, effect, None, record, str(error.get("message", ""))
        )

    # ---------------------------------------------------------------- reporting

    def summary(self) -> dict[str, Any]:
        """Aggregate counts, cost and latency for this stage's records (dispositions excluded)."""
        records = [r for r in self.records if r.source != "disposition"]
        paid = [r for r in records if r.source == "live_inference" and r.usage]
        cost = sum(
            (Decimal(str(r.usage["cost"])) for r in paid if r.usage and r.usage.get("cost")),
            Decimal(0),
        )
        latencies = [r.latency_ms for r in records if r.latency_ms is not None]
        by_effect: dict[str, int] = {}
        for record in records:
            by_effect[record.effect] = by_effect.get(record.effect, 0) + 1
        return {
            "reviews": len(records),
            "model_calls": sum(1 for r in records if r.source == "live_inference"),
            "served_from_cache": sum(1 for r in records if r.source == "cache"),
            "replayed": sum(1 for r in records if r.source == "replay"),
            "approved": sum(1 for r in records if r.decision is ReviewDecision.APPROVE),
            "rejected": sum(1 for r in records if r.decision is ReviewDecision.REJECT),
            "errors": sum(1 for r in records if r.error is not None),
            "effects": dict(sorted(by_effect.items())),
            "total_cost": str(cost),
            "cost_currency": "USD",
            "mean_latency_ms": round(sum(latencies) / len(latencies), 3) if latencies else None,
            "point_in_time_valid": self.point_in_time_valid(),
        }

    def point_in_time_valid(self) -> bool:
        """True only when every review came from a reviewer attested as point-in-time safe.

        Historical results from any other reviewer (one that may know about events after the
        decision time) must not be presented as point-in-time valid.
        """
        records = [r for r in self.records if r.source != "disposition"]
        return bool(records) and all(r.point_in_time_safe for r in records)

