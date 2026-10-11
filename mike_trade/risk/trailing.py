"""Pure, broker-independent monotonic stepped trailing-stop ratchet (LONG and SHORT).

Nothing here performs I/O or touches a broker, clock, persistence layer, or
order: every function maps explicit inputs to a value.  The module only
*computes stop candidates*; a coordinator decides whether and how to replace a
live protective order.

Model
-----
* ``initial_state`` starts with ``high_watermark == ratchet_anchor ==
  entry_reference`` and ``candidate_stop = entry_reference - distance``.
* ``advance`` raises ``high_watermark`` to ``max(previous, observed_high)``,
  counts the complete favorable steps ``(high_watermark - ratchet_anchor) //
  step`` and moves the anchor up by exactly that many whole steps (a single
  jump may cross several steps).  The candidate stop is
  ``ratchet_anchor - distance``.  Sub-step highs do not reprice.
* The watermark, anchor and candidate stop never decrease; a lower observed
  high changes nothing.  ``steps_advanced`` is the number of whole steps taken
  by the most recent ``initial_state`` (0) / ``advance`` call, not a running
  total.
* ``should_replace`` floors the candidate to the broker tick grid via an
  injected rounder *before* comparing, and answers True only when the rounded
  candidate is strictly above the last confirmed stop.

Documented basis (design decision)
----------------------------------
PERCENT distances **and** PERCENT steps are both computed on the *entry
reference*, never on the moving high watermark or anchor:
``amount = entry_reference * pct / 100``.  The resulting USD amounts are
therefore constant for the life of the position, which keeps the ratchet
deterministic and replay-stable.

SHORT side (exact mirror)
-------------------------
A short stop sits *above* the market.  ``initial_state(..., side="SHORT")`` starts with
``candidate_stop = entry_reference + distance``; ``advance`` folds in the observed *low*
(the favorable extreme of a short) into ``high_watermark`` (the lowest price seen),
moves the anchor DOWN by whole steps and sets ``candidate_stop = ratchet_anchor +
distance``.  The candidate stop, watermark and anchor never increase, so the stop ratchets
downward as price falls and never loosens upward when price rebounds.  ``should_replace``
rounds UP to the tick grid and answers True only when the rounded candidate is strictly
BELOW the last confirmed stop.

Validation: all prices and amounts must be finite ``Decimal`` values; ``float``
(and ``bool``/``int``) inputs are rejected with ``ValueError``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum

from mike_trade.domain.trading import TimeInForce

__all__ = [
    "DistanceKind",
    "SteppedTrailingPolicy",
    "SteppedTrailingState",
    "RatchetResult",
    "initial_state",
    "advance",
    "should_replace",
]

_ZERO = Decimal("0")
_HUNDRED = Decimal("100")
_LONG = "LONG"
_SHORT = "SHORT"


class DistanceKind(StrEnum):
    USD = "USD"
    PERCENT = "PERCENT"


def _decimal(value: object, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise ValueError(f"{name} must be a Decimal, not {type(value).__name__}")
    if not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
    if positive and value <= _ZERO:
        raise ValueError(f"{name} must be > 0")
    return value


def _kind(value: object, name: str) -> DistanceKind:
    try:
        return DistanceKind(value)
    except ValueError:
        raise ValueError(f"{name} must be one of {[k.value for k in DistanceKind]}") from None


def _flag(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a bool")
    return value


def _side(side: object) -> str:
    normalized = side.strip().upper() if isinstance(side, str) else side
    if normalized not in (_LONG, _SHORT):
        raise ValueError("side must be 'LONG' or 'SHORT'")
    return str(normalized)


@dataclass(frozen=True, slots=True)
class SteppedTrailingPolicy:
    """Immutable stepped-trailing configuration."""

    distance_kind: DistanceKind
    distance_value: Decimal
    step_kind: DistanceKind
    step_value: Decimal
    min_replace_interval: timedelta
    time_in_force: TimeInForce
    policy_revision: str
    requires_standalone_stop: bool = True
    requires_stop_replace: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "distance_kind", _kind(self.distance_kind, "distance_kind"))
        object.__setattr__(self, "step_kind", _kind(self.step_kind, "step_kind"))
        _decimal(self.distance_value, "distance_value", positive=True)
        _decimal(self.step_value, "step_value", positive=True)
        if not isinstance(self.min_replace_interval, timedelta) or self.min_replace_interval < timedelta(0):
            raise ValueError("min_replace_interval must be a timedelta >= 0")
        tif = self.time_in_force
        if isinstance(tif, str):
            try:
                tif = TimeInForce[tif.strip().upper()]
            except KeyError:
                raise ValueError("time_in_force must be a TimeInForce (DAY/GTC)") from None
        if not isinstance(tif, TimeInForce):
            raise ValueError("time_in_force must be a TimeInForce (DAY/GTC)")
        object.__setattr__(self, "time_in_force", tif)
        if not isinstance(self.policy_revision, str) or not self.policy_revision.strip():
            raise ValueError("policy_revision must be a non-empty string")
        object.__setattr__(self, "policy_revision", self.policy_revision.strip())
        _flag(self.requires_standalone_stop, "requires_standalone_stop")
        _flag(self.requires_stop_replace, "requires_stop_replace")

    def distance_amount(self, entry_reference: Decimal) -> Decimal:
        """Trailing distance in USD (percent is taken on the entry reference)."""

        return _amount(self.distance_kind, self.distance_value, entry_reference)

    def step_amount(self, entry_reference: Decimal) -> Decimal:
        """Step increment in USD (percent is taken on the entry reference)."""

        return _amount(self.step_kind, self.step_value, entry_reference)


def _amount(kind: DistanceKind, value: Decimal, entry_reference: Decimal) -> Decimal:
    if kind is DistanceKind.USD:
        return value
    return entry_reference * value / _HUNDRED


@dataclass(frozen=True, slots=True)
class SteppedTrailingState:
    """Ratchet state; also the result type returned by ``initial_state``/``advance``."""

    entry_reference: Decimal
    high_watermark: Decimal
    ratchet_anchor: Decimal
    candidate_stop: Decimal
    steps_advanced: int = 0
    #: ``"LONG"`` (default) or ``"SHORT"``.  For a short, ``high_watermark`` is the LOWEST
    #: price seen (the favorable extreme) and ``candidate_stop`` sits above the anchor.
    direction: str = _LONG

    def __post_init__(self) -> None:
        object.__setattr__(self, "direction", _side(self.direction))
        for name in ("entry_reference", "high_watermark", "ratchet_anchor", "candidate_stop"):
            _decimal(getattr(self, name), name, positive=True)
        if isinstance(self.steps_advanced, bool) or not isinstance(self.steps_advanced, int):
            raise ValueError("steps_advanced must be an int")
        if self.steps_advanced < 0:
            raise ValueError("steps_advanced must be >= 0")
        if self.direction == _LONG:
            if self.high_watermark < self.ratchet_anchor:
                raise ValueError("high_watermark must be >= ratchet_anchor")
            if self.candidate_stop >= self.ratchet_anchor:
                raise ValueError("candidate_stop must be below ratchet_anchor")
        else:
            if self.high_watermark > self.ratchet_anchor:
                raise ValueError("a short's watermark (lowest price) must be <= ratchet_anchor")
            if self.candidate_stop <= self.ratchet_anchor:
                raise ValueError("a short's candidate_stop must be above ratchet_anchor")


RatchetResult = SteppedTrailingState


def _check_policy(policy: object) -> SteppedTrailingPolicy:
    if not isinstance(policy, SteppedTrailingPolicy):
        raise ValueError("policy must be a SteppedTrailingPolicy")
    return policy


def initial_state(
    entry_reference: Decimal, policy: SteppedTrailingPolicy, *, side: str = _LONG
) -> SteppedTrailingState:
    """Initial stop is ``entry_reference -/+ distance`` (long/short); it must stay positive."""

    direction = _side(side)
    policy = _check_policy(policy)
    entry = _decimal(entry_reference, "entry_reference", positive=True)
    if direction == _LONG:
        stop = entry - policy.distance_amount(entry)
        if stop <= _ZERO:
            raise ValueError("trailing distance must be smaller than entry_reference")
    else:
        stop = entry + policy.distance_amount(entry)
    return SteppedTrailingState(
        entry_reference=entry,
        high_watermark=entry,
        ratchet_anchor=entry,
        candidate_stop=stop,
        steps_advanced=0,
        direction=direction,
    )


def advance(
    state: SteppedTrailingState,
    observed_high: Decimal,
    policy: SteppedTrailingPolicy,
    *,
    side: str | None = None,
) -> RatchetResult:
    """Fold the observed favorable extreme into ``state``; the stop never loosens.

    ``observed_high`` is the observed HIGH for a long and the observed LOW for a short
    (the name is kept for compatibility).  The direction comes from ``state``; an explicit
    ``side`` must agree with it.
    """

    policy = _check_policy(policy)
    if not isinstance(state, SteppedTrailingState):
        raise ValueError("state must be a SteppedTrailingState")
    if side is not None and _side(side) != state.direction:
        raise ValueError("side does not match the trailing state's direction")
    extreme = _decimal(observed_high, "observed_high", positive=True)
    entry = state.entry_reference
    step = policy.step_amount(entry)
    if state.direction == _LONG:
        watermark = max(state.high_watermark, extreme)
        steps = int((watermark - state.ratchet_anchor) // step)
        anchor = state.ratchet_anchor + step * steps
        # max() keeps the stop monotonic even if the caller swapped in a policy
        # with a wider distance between calls.
        stop = max(state.candidate_stop, anchor - policy.distance_amount(entry))
    else:
        watermark = min(state.high_watermark, extreme)
        steps = int((state.ratchet_anchor - watermark) // step)
        anchor = state.ratchet_anchor - step * steps
        stop = min(state.candidate_stop, anchor + policy.distance_amount(entry))
    return SteppedTrailingState(
        entry_reference=entry,
        high_watermark=watermark,
        ratchet_anchor=anchor,
        candidate_stop=stop,
        steps_advanced=steps,
        direction=state.direction,
    )


def should_replace(
    candidate_stop: Decimal,
    last_confirmed_stop: Decimal,
    tick_rounder: Callable[[Decimal], Decimal],
    *,
    side: str = _LONG,
) -> bool:
    """True only if the tick-rounded candidate tightens the last confirmed stop.

    LONG: ``tick_rounder`` must round DOWN; True only when the rounded candidate is strictly
    ABOVE the last confirmed stop (a rounder returning more than the candidate is rejected).
    SHORT: ``tick_rounder`` must round UP; True only when the rounded candidate is strictly
    BELOW the last confirmed stop (a rounder returning less than the candidate is rejected).
    """

    direction = _side(side)
    candidate = _decimal(candidate_stop, "candidate_stop", positive=True)
    confirmed = _decimal(last_confirmed_stop, "last_confirmed_stop", positive=True)
    if not callable(tick_rounder):
        raise ValueError("tick_rounder must be callable")
    rounded = _decimal(tick_rounder(candidate), "rounded candidate_stop")
    if direction == _LONG:
        if rounded > candidate:
            raise ValueError("tick_rounder must round down, never up")
        return rounded > confirmed
    if rounded < candidate:
        raise ValueError("tick_rounder must round up, never down for a short")
    return rounded < confirmed
