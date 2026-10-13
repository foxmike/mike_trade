"""Michelle's fill-based fixed-then-trailing protective stop (all ``Decimal``, long only).

Rules (Michelle's confirmed clarification, superseding the VWAP/ORB-reference formulas):

* ``InitialStop = ActualBuyFillPrice - $0.25``.  It is rounded **up** onto the exchange tick
  grid, so rounding can only make the initial distance smaller than $0.25, never larger.
* ``Candidate = HighestTradedPriceSinceFill * (1 - 0.2%)``.  It is rounded **down** onto the
  tick grid (a long stop is never made tighter than designed), which is the rounding every
  other trailing policy in the engine already uses.
* The stop stays at ``InitialStop`` until the tick-adjusted candidate is **strictly greater**
  than ``InitialStop`` (equality does not activate).  From then on
  ``ActiveStop = max(PreviousActiveStop, Candidate)``; it never decreases and never returns to
  the fixed level.

Worked example: fill 428.9944 -> raw initial 428.7444 -> 428.75.  Fill 400.00 -> initial 399.75;
at a 401.00 high the candidate is 400.198 -> 400.19 > 399.75, so the stop becomes 400.19.

The initial stop is a stop *level*, not a guaranteed exit price: gaps and execution slippage can
realize more than $0.25 per share of loss.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from mike_trade.domain.protection import ProtectionKind, ProtectionPolicy
from mike_trade.risk.fixed_stop import ALPACA_EQUITY_TICK_RULES, TickRules

MICHELLE_INITIAL_STOP_OFFSET = Decimal("0.25")
MICHELLE_TRAIL_PERCENT = Decimal("0.2")

#: Each Michelle BUY is protected by its same-stem SELL policy; both share the identical rule.
MATCHING_SELL = {
    "five_minute_orb_michelle_buy": "five_minute_orb_michelle_sell",
    "vwap_bounce_break_michelle_buy": "vwap_bounce_break_michelle_sell",
}

_ZERO = Decimal(0)
_HUNDRED = Decimal(100)


def protection_applies(buy_strategy_id: object, sell_strategy_ids: Iterable[str]) -> bool:
    """Whether ``buy_strategy_id`` is a Michelle BUY whose matching SELL policy is configured."""
    matching = MATCHING_SELL.get(str(buy_strategy_id))
    return matching is not None and matching in set(sell_strategy_ids)


def initial_stop(
    fill_price: Decimal, rules: TickRules = ALPACA_EQUITY_TICK_RULES
) -> Decimal | None:
    """``fill - $0.25`` rounded up to the tick grid; ``None`` when no valid stop exists.

    A stop must be strictly positive and strictly below the fill.
    """
    if not isinstance(fill_price, Decimal) or not fill_price.is_finite() or fill_price <= _ZERO:
        return None
    raw = fill_price - MICHELLE_INITIAL_STOP_OFFSET
    if raw <= _ZERO:
        return None  # no positive stop exists below this fill
    stop = rules.ceil(raw)
    return stop if _ZERO < stop < fill_price else None


def trailing_candidate(highest: Decimal, rules: TickRules = ALPACA_EQUITY_TICK_RULES) -> Decimal:
    """``highest * (1 - 0.2%)`` rounded down to the tick grid."""
    return rules.floor(highest * (Decimal(1) - MICHELLE_TRAIL_PERCENT / _HUNDRED))


def next_stop(
    initial: Decimal,
    previous: Decimal,
    highest: Decimal,
    rules: TickRules = ALPACA_EQUITY_TICK_RULES,
) -> Decimal:
    """The active stop after observing ``highest``; never below ``previous`` or ``initial``."""
    candidate = trailing_candidate(highest, rules)
    stop = max(previous, initial)
    return max(stop, candidate) if candidate > initial else stop


def policy_for_fill(
    fill_price: Decimal, rules: TickRules = ALPACA_EQUITY_TICK_RULES
) -> ProtectionPolicy | None:
    """The protection policy for one confirmed BUY fill, or ``None`` if no valid stop exists.

    ``activation_reference`` is the (tick-valid) initial stop itself: the trail activates only
    when its tick-adjusted candidate strictly exceeds it.
    """
    stop = initial_stop(fill_price, rules)
    if stop is None:
        return None
    return ProtectionPolicy(
        ProtectionKind.FIXED_THEN_TRAILING_STOP,
        trail_percent=MICHELLE_TRAIL_PERCENT,
        initial_stop_price=stop,
        activation_reference=stop,
        require_broker_held=True,
    )


def is_current_policy(policy: ProtectionPolicy) -> bool:
    """Whether ``policy`` has the fill-based semantics (guards against restored legacy policies)."""
    return (
        policy.kind is ProtectionKind.FIXED_THEN_TRAILING_STOP
        and policy.trail_percent == MICHELLE_TRAIL_PERCENT
        and policy.initial_stop_price is not None
        and policy.activation_reference == policy.initial_stop_price
    )
