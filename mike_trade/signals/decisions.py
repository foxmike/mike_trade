"""Deterministic, auditable exit-signal arbitration (valid vs rejected decisions).

``arbitrate_exit_decisions`` is a pure function: it reads the candidate exit
requests raised for one position on one evaluation tick, the confirmed position
snapshot, the minimum-hold configuration and an injected evaluation time, and
returns an immutable :class:`ArbitrationResult`. It never submits orders, never
touches a broker, and never reads a clock, file, network or database. The
result is advisory; the execution layer decides what to do with it.

Policy (first valid exit, no voting)
------------------------------------
* Candidates are ranked by ``EXIT_PRIORITY`` (protective stop, manual close,
  hard risk, forced liquidation, then strategy SELL). Non-strategy exits bypass
  minimum hold; ``STRATEGY_SELL`` is subject to it.
* The stable tie-break is ``(priority, completed-bar timestamp, strategy_id,
  signal_id)`` ascending; a missing bar time or strategy id sorts first. Input
  order never matters. There is no weighting and no confidence score anywhere.
* A candidate is *valid* only if no rejection reason applies. Every applicable
  reason is recorded, in the fixed ``DecisionReason`` declaration order. The
  first valid candidate in tie-break order is SELECTED; every other valid
  candidate is REJECTED as superseded (``SUPERSEDED_BY_HIGHER_PRIORITY`` when
  the winner has a strictly better priority, ``SUPERSEDED_BY_TIE_BREAK`` when
  the priorities are equal).
* A flat, unmanaged (or merely adopted), or non-long position selects nothing;
  a SELL never becomes a short or an opposite-side order.
* Pending exit: ``pending_exit_source`` names the source of an exit that is
  already working (including a broker-held stop fill in progress). Any
  candidate that is not of strictly higher priority than it is rejected with
  ``EXIT_ALREADY_PENDING``, so overlapping sells cannot oversell. A strictly
  higher-priority exit (e.g. a protective stop while a strategy SELL is
  pending) is still selected; the execution layer must reconcile the pending
  order (cancel/replace) before acting on it.
* The selected decision always targets 100% of the confirmed remaining
  quantity (``Position.quantity``); partial exits do not exist.
* Duplicate suppression across ticks belongs to ``signals.rearm``; callers
  pass the signal ids it has already seen as ``seen_signal_ids``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum

from mike_trade.domain.trading import ManagementStatus, Position, PositionDirection
from mike_trade.strategies.sell.base import (
    EXIT_PRIORITY,
    ExitLongRecommendation,
    ExitSource,
    min_hold_applies,
)

__all__ = [
    "DECISION_SCHEMA_VERSION",
    "DecisionOutcome",
    "DecisionReason",
    "ExitCandidate",
    "DecisionRecord",
    "ArbitrationResult",
    "arbitrate_exit_decisions",
]

DECISION_SCHEMA_VERSION = 1
_MIN_TIME = datetime.min.replace(tzinfo=UTC)


class DecisionOutcome(Enum):
    """Arbitration disposition of one candidate."""

    SELECTED = "SELECTED"
    REJECTED = "REJECTED"


class DecisionReason(Enum):
    """Explicit reason codes; declaration order is the recorded order."""

    SELECTED = "SELECTED"
    POSITION_FLAT = "POSITION_FLAT"
    NOT_MANAGED = "NOT_MANAGED"
    NOT_LONG = "NOT_LONG"
    SYMBOL_MISMATCH = "SYMBOL_MISMATCH"
    CYCLE_MISMATCH = "CYCLE_MISMATCH"
    DUPLICATE_SIGNAL = "DUPLICATE_SIGNAL"
    REPLAY_SUPPRESSED = "REPLAY_SUPPRESSED"
    REARM_REQUIRED = "REARM_REQUIRED"
    STRATEGY_DISABLED = "STRATEGY_DISABLED"
    STALE_SIGNAL = "STALE_SIGNAL"
    EXIT_ALREADY_PENDING = "EXIT_ALREADY_PENDING"
    MIN_HOLD_ACTIVE = "MIN_HOLD_ACTIVE"
    SUPERSEDED_BY_HIGHER_PRIORITY = "SUPERSEDED_BY_HIGHER_PRIORITY"
    SUPERSEDED_BY_TIE_BREAK = "SUPERSEDED_BY_TIE_BREAK"


def _utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty str")
    return value.strip()


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class ExitCandidate:
    """One exit request for a position on one tick (stop, manual, risk, forced, SELL).

    ``strategy_id``/``version`` are required for ``STRATEGY_SELL`` and absent for
    every other source. ``enabled`` and ``stale`` are facts supplied by the
    caller (rule enablement, data freshness); they are not inferred here.
    """

    signal_id: str
    source: ExitSource
    symbol: str
    strategy_id: str | None = None
    version: str | None = None
    position_cycle_id: str | None = None
    bar_time: datetime | None = None
    requested_at: datetime | None = None
    enabled: bool = True
    stale: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "signal_id", _text(self.signal_id, "signal_id"))
        if not isinstance(self.source, ExitSource):
            raise ValueError("source must be ExitSource")
        object.__setattr__(self, "symbol", _text(self.symbol, "symbol").upper())
        if min_hold_applies(self.source):
            object.__setattr__(self, "strategy_id", _text(self.strategy_id, "strategy_id"))
            object.__setattr__(self, "version", _text(self.version, "version"))
        elif self.strategy_id is not None or self.version is not None:
            raise ValueError("strategy_id/version are only valid for STRATEGY_SELL")
        if self.position_cycle_id is not None:
            object.__setattr__(
                self, "position_cycle_id", _text(self.position_cycle_id, "position_cycle_id")
            )
        for name in ("bar_time", "requested_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _utc(value, name))
        if not isinstance(self.enabled, bool) or not isinstance(self.stale, bool):
            raise ValueError("enabled and stale must be bool")

    @classmethod
    def from_recommendation(
        cls,
        recommendation: ExitLongRecommendation,
        *,
        enabled: bool = True,
        stale: bool = False,
        requested_at: datetime | None = None,
    ) -> ExitCandidate:
        """Wrap a SELL strategy recommendation as a STRATEGY_SELL candidate."""
        if not isinstance(recommendation, ExitLongRecommendation):
            raise ValueError("recommendation must be ExitLongRecommendation")
        return cls(
            signal_id=recommendation.signal_id,
            source=ExitSource.STRATEGY_SELL,
            symbol=recommendation.symbol,
            strategy_id=recommendation.strategy_id,
            version=recommendation.version,
            position_cycle_id=recommendation.position_cycle_id,
            bar_time=recommendation.bar_time,
            requested_at=requested_at,
            enabled=enabled,
            stale=stale,
        )

    def sort_key(self) -> tuple[int, datetime, str, str, str]:
        """Stable tie-break: priority, bar time, strategy_id, signal_id (then source)."""
        return (
            EXIT_PRIORITY[self.source],
            self.bar_time or _MIN_TIME,
            self.strategy_id or "",
            self.signal_id,
            self.source.value,
        )


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """One audit entry per candidate. Carries no confidence or score field."""

    decision_id: str
    signal_id: str
    source: ExitSource
    strategy_id: str | None
    version: str | None
    symbol: str
    position_cycle_id: str | None
    outcome: DecisionOutcome
    reason_codes: tuple[DecisionReason, ...]
    decided_at: datetime
    bar_time: datetime | None
    requested_at: datetime | None
    quantity: Decimal | None
    superseded_by: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, DecisionOutcome):
            raise ValueError("outcome must be DecisionOutcome")
        if not self.reason_codes or any(
            not isinstance(code, DecisionReason) for code in self.reason_codes
        ):
            raise ValueError("reason_codes must be a non-empty tuple of DecisionReason")
        selected = self.outcome is DecisionOutcome.SELECTED
        if selected != (self.quantity is not None):
            raise ValueError("only SELECTED decisions carry a target quantity")
        if selected and DecisionReason.SELECTED not in self.reason_codes:
            raise ValueError("SELECTED decisions require the SELECTED reason")
        if not selected and DecisionReason.SELECTED in self.reason_codes:
            raise ValueError("REJECTED decisions must not carry the SELECTED reason")

    def to_dict(self) -> dict[str, object]:
        """JSON-safe, deterministic mapping for an append-only audit log."""
        return {
            "schema_version": DECISION_SCHEMA_VERSION,
            "decision_id": self.decision_id,
            "signal_id": self.signal_id,
            "source": self.source.value,
            "strategy_id": self.strategy_id,
            "version": self.version,
            "symbol": self.symbol,
            "position_cycle_id": self.position_cycle_id,
            "outcome": self.outcome.value,
            "reason_codes": [code.value for code in self.reason_codes],
            "decided_at": _iso(self.decided_at),
            "bar_time": _iso(self.bar_time),
            "requested_at": _iso(self.requested_at),
            "quantity": None if self.quantity is None else str(self.quantity),
            "superseded_by": self.superseded_by,
        }


@dataclass(frozen=True, slots=True)
class ArbitrationResult:
    """Immutable outcome: at most one selected exit plus every candidate's record."""

    symbol: str
    position_cycle_id: str | None
    evaluated_at: datetime
    selected: DecisionRecord | None
    records: tuple[DecisionRecord, ...]

    def __post_init__(self) -> None:
        selected = [r for r in self.records if r.outcome is DecisionOutcome.SELECTED]
        if len(selected) > 1:
            raise ValueError("at most one exit may be selected")
        if (self.selected is None) != (not selected) or (
            self.selected is not None and self.selected is not selected[0]
        ):
            raise ValueError("selected must be the SELECTED record, or None")

    @property
    def rejected(self) -> tuple[DecisionRecord, ...]:
        """Records of every non-selected candidate."""
        return tuple(r for r in self.records if r.outcome is DecisionOutcome.REJECTED)

    def to_dict(self) -> dict[str, object]:
        """JSON-safe, deterministic mapping for an append-only audit log."""
        return {
            "schema_version": DECISION_SCHEMA_VERSION,
            "symbol": self.symbol,
            "position_cycle_id": self.position_cycle_id,
            "evaluated_at": _iso(self.evaluated_at),
            "selected_decision_id": None if self.selected is None else self.selected.decision_id,
            "records": [record.to_dict() for record in self.records],
        }


def _decision_id(
    candidate: ExitCandidate,
    cycle_id: str | None,
    evaluated_at: datetime,
    outcome: DecisionOutcome,
    reasons: tuple[DecisionReason, ...],
) -> str:
    canonical = json.dumps(
        [
            DECISION_SCHEMA_VERSION,
            candidate.signal_id,
            candidate.source.value,
            candidate.strategy_id,
            candidate.version,
            candidate.symbol,
            cycle_id,
            _iso(evaluated_at),
            outcome.value,
            [code.value for code in reasons],
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def arbitrate_exit_decisions(
    candidates: Iterable[ExitCandidate],
    *,
    symbol: str,
    position: Position | None,
    evaluation_time: datetime,
    minimum_hold: timedelta = timedelta(0),
    entry_filled_at: datetime | None = None,
    direction: PositionDirection = PositionDirection.LONG,
    pending_exit_source: ExitSource | None = None,
    seen_signal_ids: Collection[str] = frozenset(),
) -> ArbitrationResult:
    """Select at most one full-quantity exit and record every candidate's fate.

    ``position`` is the confirmed snapshot (``None`` means flat); its
    ``quantity`` is the confirmed remaining quantity. ``entry_filled_at`` is
    required whenever a STRATEGY_SELL candidate is present and ``minimum_hold``
    is positive. ``pending_exit_source`` is the source of an exit already in
    flight, if any. ``seen_signal_ids`` are ids already consumed on earlier
    ticks (see ``signals.rearm``).
    """
    symbol = _text(symbol, "symbol").upper()
    evaluated_at = _utc(evaluation_time, "evaluation_time")
    if not isinstance(minimum_hold, timedelta) or minimum_hold < timedelta(0):
        raise ValueError("minimum_hold must be a non-negative timedelta")
    if position is not None and not isinstance(position, Position):
        raise ValueError("position must be a Position or None")
    if not isinstance(direction, PositionDirection):
        raise ValueError("direction must be PositionDirection")
    if pending_exit_source is not None and not isinstance(pending_exit_source, ExitSource):
        raise ValueError("pending_exit_source must be ExitSource or None")
    supplied = tuple(candidates)
    if any(not isinstance(c, ExitCandidate) for c in supplied):
        raise ValueError("candidates must be ExitCandidate instances")
    ordered = sorted(supplied, key=ExitCandidate.sort_key)

    cycle_id = position.cycle_id if position is not None else None
    held_for_hold_check: timedelta | None = None
    if (
        position is not None
        and minimum_hold > timedelta(0)
        and any(min_hold_applies(c.source) for c in ordered)
    ):
        if entry_filled_at is None:
            raise ValueError("minimum-hold evaluation requires entry_filled_at")
        held_for_hold_check = evaluated_at - _utc(entry_filled_at, "entry_filled_at")

    position_reasons: list[DecisionReason] = []
    if position is None:
        position_reasons.append(DecisionReason.POSITION_FLAT)
    else:
        if position.symbol != symbol:
            raise ValueError("position symbol does not match symbol")
        if position.management is not ManagementStatus.MANAGED or position.cycle_id is None:
            position_reasons.append(DecisionReason.NOT_MANAGED)
        if direction is not PositionDirection.LONG:
            position_reasons.append(DecisionReason.NOT_LONG)

    seen = set(seen_signal_ids)
    # (candidate, reasons) in tie-break order; empty reasons means valid.
    evaluated: list[tuple[ExitCandidate, list[DecisionReason]]] = []
    for candidate in ordered:
        reasons: list[DecisionReason] = list(position_reasons)
        if candidate.symbol != symbol:
            reasons.append(DecisionReason.SYMBOL_MISMATCH)
        if (
            position is not None
            and candidate.position_cycle_id is not None
            and candidate.position_cycle_id != position.cycle_id
        ):
            reasons.append(DecisionReason.CYCLE_MISMATCH)
        if candidate.signal_id in seen:
            reasons.append(DecisionReason.DUPLICATE_SIGNAL)
        seen.add(candidate.signal_id)
        if not candidate.enabled:
            reasons.append(DecisionReason.STRATEGY_DISABLED)
        if candidate.stale:
            reasons.append(DecisionReason.STALE_SIGNAL)
        if pending_exit_source is not None and (
            EXIT_PRIORITY[candidate.source] >= EXIT_PRIORITY[pending_exit_source]
        ):
            reasons.append(DecisionReason.EXIT_ALREADY_PENDING)
        if (
            held_for_hold_check is not None
            and min_hold_applies(candidate.source)
            and held_for_hold_check < minimum_hold
        ):
            reasons.append(DecisionReason.MIN_HOLD_ACTIVE)
        evaluated.append((candidate, reasons))

    winner = next((c for c, reasons in evaluated if not reasons), None)

    records: list[DecisionRecord] = []
    selected: DecisionRecord | None = None
    for candidate, reasons in evaluated:
        superseded_by: str | None = None
        quantity: Decimal | None = None
        if reasons:
            outcome = DecisionOutcome.REJECTED
            codes = tuple(reasons)
        elif candidate is winner:
            assert position is not None
            outcome = DecisionOutcome.SELECTED
            codes = (DecisionReason.SELECTED,)
            quantity = position.quantity
        else:
            assert winner is not None
            outcome = DecisionOutcome.REJECTED
            superseded_by = winner.signal_id
            codes = (
                (DecisionReason.SUPERSEDED_BY_HIGHER_PRIORITY,)
                if EXIT_PRIORITY[winner.source] < EXIT_PRIORITY[candidate.source]
                else (DecisionReason.SUPERSEDED_BY_TIE_BREAK,)
            )
        record = DecisionRecord(
            decision_id=_decision_id(candidate, cycle_id, evaluated_at, outcome, codes),
            signal_id=candidate.signal_id,
            source=candidate.source,
            strategy_id=candidate.strategy_id,
            version=candidate.version,
            symbol=candidate.symbol,
            position_cycle_id=cycle_id or candidate.position_cycle_id,
            outcome=outcome,
            reason_codes=codes,
            decided_at=evaluated_at,
            bar_time=candidate.bar_time,
            requested_at=candidate.requested_at,
            quantity=quantity,
            superseded_by=superseded_by,
        )
        records.append(record)
        if outcome is DecisionOutcome.SELECTED:
            selected = record
    return ArbitrationResult(
        symbol=symbol,
        position_cycle_id=cycle_id,
        evaluated_at=evaluated_at,
        selected=selected,
        records=tuple(records),
    )
