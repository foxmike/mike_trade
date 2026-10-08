"""Restart-stable rearm and duplicate suppression for strategy signals.

``RearmState`` is an immutable record keyed by ``(strategy_id, version, symbol,
side)``. It remembers the last emitted ``signal_id``, the last triggering
completed-bar timestamp, a bounded window of recently seen signal ids, the
arm/disarm status, the current position-cycle id, and an opaque, JSON-safe
per-strategy persistent state (for example the rsi_reversal ``armed`` flag).

Everything here is pure: transitions return new states, nothing reads a clock,
and there is no database or file I/O. ``to_dict``/``from_dict`` give a
deterministic, versioned mapping the existing persistence layer can store, so a
rehydrated state suppresses exactly the replays the original did.

Suppression rules for :func:`check_and_record`, evaluated in this order:

1. ``DUPLICATE_SIGNAL``: the ``signal_id`` equals the last emitted id or is in
   the seen window.
2. ``REPLAY_SUPPRESSED``: the bar is not strictly newer than the last
   triggering bar (same-bar replays and out-of-order older bars).
3. ``REARM_REQUIRED``: only when ``RearmPolicy.disarm_on_emit`` is set and the
   state is disarmed.

Cycle reset (:func:`reset_cycle`) marks a new entry cycle: the state is rearmed
and any per-cycle disarm is cleared, but the last-bar watermark and seen ids
are kept, so a replay of an old bar is still suppressed while any genuinely
newer bar can fire in the new cycle. Resetting to the cycle already recorded is
a no-op, so replaying the reset after a restart cannot rearm twice.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import MappingProxyType

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.signals.decisions import DecisionReason
from mike_trade.strategies.base import make_signal_id

__all__ = [
    "REARM_STATE_VERSION",
    "RearmPolicy",
    "RearmState",
    "RearmCheck",
    "check_and_record",
    "reset_cycle",
]

REARM_STATE_VERSION = 1


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty str")
    return value.strip()


def _utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _freeze(value: object, field: str) -> object:
    """Deep-copy JSON-safe data into immutable containers; reject anything else."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"{field} must be finite")
        return value
    if isinstance(value, Mapping):
        items: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{field} keys must be str")
            items[key] = _freeze(item, f"{field}.{key}")
        return MappingProxyType(dict(sorted(items.items())))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item, f"{field}[]") for item in value)
    raise ValueError(f"{field} must be JSON-safe; got {type(value).__name__}")


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class RearmPolicy:
    """Knobs for suppression; defaults match strategies that own their own latch.

    ``max_seen_signal_ids`` bounds the persisted id window (oldest dropped
    first; the bar watermark still blocks older replays). ``disarm_on_emit``
    makes the generic layer disarm after every accepted signal until
    :func:`reset_cycle` or ``RearmState.rearmed`` is applied.
    """

    max_seen_signal_ids: int = 32
    disarm_on_emit: bool = False

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_seen_signal_ids, bool)
            or not isinstance(self.max_seen_signal_ids, int)
            or self.max_seen_signal_ids < 1
        ):
            raise ValueError("max_seen_signal_ids must be an int >= 1")
        if not isinstance(self.disarm_on_emit, bool):
            raise ValueError("disarm_on_emit must be bool")


DEFAULT_REARM_POLICY = RearmPolicy()


@dataclass(frozen=True, slots=True)
class RearmState:
    """Immutable suppression state for one (strategy_id, version, symbol, side)."""

    strategy_id: str
    version: str
    symbol: str
    side: StrategyRole
    last_signal_id: str | None = None
    last_bar_time: datetime | None = None
    armed: bool = True
    cycle_id: str | None = None
    seen_signal_ids: tuple[str, ...] = ()
    persistent_state: Mapping[str, object] = MappingProxyType({})

    def __post_init__(self) -> None:
        object.__setattr__(self, "strategy_id", _text(self.strategy_id, "strategy_id"))
        object.__setattr__(self, "version", _text(self.version, "version"))
        object.__setattr__(self, "symbol", _text(self.symbol, "symbol").upper())
        if not isinstance(self.side, StrategyRole):
            raise ValueError("side must be StrategyRole")
        if (self.last_signal_id is None) != (self.last_bar_time is None):
            raise ValueError("last_signal_id and last_bar_time are set together")
        if self.last_signal_id is not None:
            object.__setattr__(self, "last_signal_id", _text(self.last_signal_id, "last_signal_id"))
            object.__setattr__(self, "last_bar_time", _utc(self.last_bar_time, "last_bar_time"))
        if not isinstance(self.armed, bool):
            raise ValueError("armed must be bool")
        if self.cycle_id is not None:
            object.__setattr__(self, "cycle_id", _text(self.cycle_id, "cycle_id"))
        if not isinstance(self.seen_signal_ids, tuple):
            raise ValueError("seen_signal_ids must be a tuple")
        object.__setattr__(
            self,
            "seen_signal_ids",
            tuple(_text(item, "seen_signal_ids item") for item in self.seen_signal_ids),
        )
        if not isinstance(self.persistent_state, Mapping):
            raise ValueError("persistent_state must be a mapping")
        object.__setattr__(
            self, "persistent_state", _freeze(self.persistent_state, "persistent_state")
        )

    @property
    def key(self) -> tuple[str, str, str, str]:
        """The (strategy_id, version, symbol, side) identity of this state."""
        return (self.strategy_id, self.version, self.symbol, self.side.value)

    def with_persistent_state(self, persistent_state: Mapping[str, object]) -> RearmState:
        """Return a copy holding the strategy's new opaque persistent state."""
        return replace(self, persistent_state=persistent_state)

    def rearmed(self) -> RearmState:
        """Return a copy that is armed again (watermark and seen ids are kept)."""
        return replace(self, armed=True)

    def to_dict(self) -> dict[str, object]:
        """Deterministic, versioned, JSON-safe mapping (no I/O)."""
        return {
            "schema_version": REARM_STATE_VERSION,
            "strategy_id": self.strategy_id,
            "version": self.version,
            "symbol": self.symbol,
            "side": self.side.value,
            "last_signal_id": self.last_signal_id,
            "last_bar_time": None if self.last_bar_time is None else _iso(self.last_bar_time),
            "armed": self.armed,
            "cycle_id": self.cycle_id,
            "seen_signal_ids": list(self.seen_signal_ids),
            "persistent_state": _thaw(self.persistent_state),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> RearmState:
        """Rehydrate a state written by :meth:`to_dict`; unknown versions are rejected."""
        if not isinstance(data, Mapping):
            raise ValueError("data must be a mapping")
        if data.get("schema_version") != REARM_STATE_VERSION:
            raise ValueError(
                f"unsupported rearm state schema_version: {data.get('schema_version')!r}"
            )
        raw_bar = data.get("last_bar_time")
        if raw_bar is not None and not isinstance(raw_bar, str):
            raise ValueError("last_bar_time must be an ISO string or None")
        seen = data.get("seen_signal_ids", [])
        if not isinstance(seen, (list, tuple)):
            raise ValueError("seen_signal_ids must be a list")
        try:
            side = StrategyRole(data.get("side"))
            bar = None if raw_bar is None else datetime.fromisoformat(raw_bar)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"invalid rearm state: {exc}") from None
        return cls(
            strategy_id=data.get("strategy_id"),  # type: ignore[arg-type]
            version=data.get("version"),  # type: ignore[arg-type]
            symbol=data.get("symbol"),  # type: ignore[arg-type]
            side=side,
            last_signal_id=data.get("last_signal_id"),  # type: ignore[arg-type]
            last_bar_time=bar,
            armed=data.get("armed", True),  # type: ignore[arg-type]
            cycle_id=data.get("cycle_id"),  # type: ignore[arg-type]
            seen_signal_ids=tuple(seen),
            persistent_state=data.get("persistent_state", {}),  # type: ignore[arg-type]
        )

    def to_json(self) -> str:
        """Canonical JSON text of :meth:`to_dict` (sorted keys, compact)."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class RearmCheck:
    """Result of :func:`check_and_record`; ``state`` is unchanged when suppressed."""

    accepted: bool
    state: RearmState
    reasons: tuple[DecisionReason, ...] = ()


def check_and_record(
    state: RearmState,
    *,
    signal_id: str,
    bar_time: datetime,
    policy: RearmPolicy = DEFAULT_REARM_POLICY,
) -> RearmCheck:
    """Accept a new signal and record it, or suppress it with explicit reasons.

    ``signal_id`` must be the deterministic id for this state's strategy,
    version, symbol and side at ``bar_time``; a mismatch is a caller bug and
    raises ``ValueError`` rather than being silently suppressed or accepted.
    """
    if not isinstance(state, RearmState) or not isinstance(policy, RearmPolicy):
        raise ValueError("state must be RearmState and policy RearmPolicy")
    bar = _utc(bar_time, "bar_time")
    signal_id = _text(signal_id, "signal_id")
    expected = make_signal_id(state.strategy_id, state.version, state.symbol, state.side, bar)
    if signal_id != expected:
        raise ValueError("signal_id does not match the state key and bar_time")

    reasons: list[DecisionReason] = []
    if signal_id == state.last_signal_id or signal_id in state.seen_signal_ids:
        reasons.append(DecisionReason.DUPLICATE_SIGNAL)
    if state.last_bar_time is not None and bar <= state.last_bar_time:
        reasons.append(DecisionReason.REPLAY_SUPPRESSED)
    if not reasons and policy.disarm_on_emit and not state.armed:
        reasons.append(DecisionReason.REARM_REQUIRED)
    if reasons:
        return RearmCheck(False, state, tuple(reasons))

    seen = (*state.seen_signal_ids, signal_id)[-policy.max_seen_signal_ids :]
    return RearmCheck(
        True,
        replace(
            state,
            last_signal_id=signal_id,
            last_bar_time=bar,
            armed=False if policy.disarm_on_emit else state.armed,
            seen_signal_ids=seen,
        ),
    )


def reset_cycle(
    state: RearmState,
    *,
    cycle_id: str,
    persistent_state: Mapping[str, object] | None = None,
) -> RearmState:
    """Start a new position cycle without forgetting what was already replayed.

    Rearms the state and records ``cycle_id``; the last-bar watermark and seen
    ids are kept so old-bar replays stay suppressed. ``persistent_state``
    replaces the opaque strategy state when supplied (the strategy knows which
    of its own keys are per-cycle); otherwise it is preserved. Calling again
    with the cycle already recorded returns ``state`` unchanged.
    """
    if not isinstance(state, RearmState):
        raise ValueError("state must be RearmState")
    new_cycle = _text(cycle_id, "cycle_id")
    if new_cycle == state.cycle_id:
        return state
    updated = replace(state, cycle_id=new_cycle, armed=True)
    if persistent_state is not None:
        updated = updated.with_persistent_state(persistent_state)
    return updated
