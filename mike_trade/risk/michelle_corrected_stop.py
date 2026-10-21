"""Michelle's corrected two-stage protection (business rules V2: 42-46), all ``Decimal``, long only.

* **Stage 1 (Rule 42)** - ``InitialStop = Fill - $0.75``.  Fixed: it never follows VWAP and is
  not the 0.5% trailing policy.  A stop is a price level on the exchange tick grid; trade prices
  sit on that grid too, so ``price <= Fill - 0.75`` is exactly ``price <= floor_tick(Fill - 0.75)``
  and the level is rounded DOWN (the trigger set is identical to the rule, never earlier).
* **Stage 2 (Rule 43)** - once the market trades at or above ``Fill + $0.50`` the protective stop
  moves to breakeven (``Fill``; again ``floor_tick`` is the identical trigger set) and the EMA9
  trailing exit is armed.
* **After Stage 2 (Rule 44)** the whole position exits the instant EITHER the market touches or
  falls below breakeven (``price <= Fill``) OR crosses strictly below the EMA9 of the last
  COMPLETED one-minute candle (``price < EMA9``).  An intrabar breach is an immediate exit; no
  waiting for the candle to close.
* The breakeven floor never moves down (Rule 45) and every exit closes 100% (Rule 46).

Stage 1 and the breakeven stop are broker-representable (a fixed STOP order, replaced once, upward).
The EMA9 leg is *application-managed* because its level changes both ways each minute: it is
evaluated by the corrected SELL strategy on every trade / completed bar, and the backtest
simulator emulates it with a resting stop at :func:`effective_stop_level`.

The levels are stop *levels*, not guaranteed exit prices: gaps and slippage can realise more.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
from enum import StrEnum

from mike_trade.domain.protection import ProtectionKind, ProtectionPolicy
from mike_trade.risk.fixed_stop import ALPACA_EQUITY_TICK_RULES, TickRules

INITIAL_STOP_OFFSET = Decimal("0.75")
ACTIVATION_GAIN = Decimal("0.50")

#: Corrected BUY -> its protective SELL.  Both must be configured for the protection to apply.
CORRECTED_BUY_ID = "vwap_bounce_break_michelle_corrected_buy"
CORRECTED_SELL_ID = "vwap_bounce_break_michelle_corrected_sell"
MATCHING_SELL = {CORRECTED_BUY_ID: CORRECTED_SELL_ID}

_ZERO = Decimal(0)


class ExitTrigger(StrEnum):
    BREAKEVEN = "BREAKEVEN"  # Rule 44: price <= Fill
    EMA9 = "EMA9"  # Rule 44: price < EMA9 of the last completed candle
    INITIAL_STOP = "INITIAL_STOP"  # Rule 42


def protection_applies(buy_strategy_id: object, sell_strategy_ids: Iterable[str]) -> bool:
    matching = MATCHING_SELL.get(str(buy_strategy_id))
    return matching is not None and matching in set(sell_strategy_ids)


def _valid_fill(fill: object) -> bool:
    return isinstance(fill, Decimal) and fill.is_finite() and fill > _ZERO


def initial_stop(fill_price: Decimal, rules: TickRules = ALPACA_EQUITY_TICK_RULES) -> Decimal | None:
    """``Fill - 0.75`` on the tick grid (rounded down); ``None`` when no valid stop exists."""
    if not _valid_fill(fill_price):
        return None
    raw = fill_price - INITIAL_STOP_OFFSET
    if raw <= _ZERO:
        return None
    stop = rules.floor(raw)
    return stop if _ZERO < stop < fill_price else None


def activation_price(fill_price: Decimal) -> Decimal:
    """``Fill + 0.50`` - an exact comparison level, never rounded."""
    return fill_price + ACTIVATION_GAIN


def breakeven_stop(fill_price: Decimal, rules: TickRules = ALPACA_EQUITY_TICK_RULES) -> Decimal | None:
    """``Fill`` on the tick grid (rounded down: ``price <= Fill`` is ``price <= floor_tick(Fill)``)."""
    if not _valid_fill(fill_price):
        return None
    stop = rules.floor(fill_price)
    return stop if stop > _ZERO else None


def ema9_stop_level(ema9: Decimal, rules: TickRules = ALPACA_EQUITY_TICK_RULES) -> Decimal:
    """Highest grid price strictly below ``ema9`` - the level at which ``price < EMA9`` first holds.

    A stop order triggers at ``price <= level``; ``price < ema9`` on a tick grid is therefore
    ``price <= ceil_tick(ema9) - tick`` (this stays strictly below ``ema9`` even when ``ema9``
    sits exactly on a tick).
    """
    top = rules.ceil(ema9)
    return top - rules.increment_for(top) if top >= ema9 else top


def policy_for_fill(
    fill_price: Decimal, rules: TickRules = ALPACA_EQUITY_TICK_RULES
) -> ProtectionPolicy | None:
    """The corrected protection policy for one confirmed fill, or ``None`` if no valid stop."""
    initial = initial_stop(fill_price, rules)
    breakeven = breakeven_stop(fill_price, rules)
    if initial is None or breakeven is None or not initial < breakeven:
        return None
    return ProtectionPolicy(
        ProtectionKind.FIXED_THEN_BREAKEVEN_STOP,
        initial_stop_price=initial,
        activation_reference=activation_price(fill_price),
        breakeven_price=breakeven,
        require_broker_held=True,
    )


def is_current_policy(policy: ProtectionPolicy) -> bool:
    return policy.kind is ProtectionKind.FIXED_THEN_BREAKEVEN_STOP


def stage_two_reached(highest_price_since_fill: Decimal | None, fill_price: Decimal) -> bool:
    """Rule 43: the market traded at or above ``Fill + 0.50`` after the fill."""
    return highest_price_since_fill is not None and highest_price_since_fill >= activation_price(
        fill_price
    )


def exit_trigger(
    price: Decimal,
    fill_price: Decimal,
    ema9_last_completed: Decimal | None,
    *,
    stage_two: bool,
    rules: TickRules = ALPACA_EQUITY_TICK_RULES,
) -> ExitTrigger | None:
    """Which protective rule (if any) an observed ``price`` breaches right now.

    Stage 1 is checked against the initial stop level first; Stage 2 adds breakeven and EMA9.
    The caller supplies the Stage 2 flag from the highest price seen since the fill, so the
    activation print itself never also counts as a breach.
    """
    initial = initial_stop(fill_price, rules)
    if initial is not None and price <= initial:
        return ExitTrigger.INITIAL_STOP
    if not stage_two:
        return None
    if price <= fill_price:
        return ExitTrigger.BREAKEVEN
    if ema9_last_completed is not None and price < ema9_last_completed:
        return ExitTrigger.EMA9
    return None


def effective_stop_level(
    fill_price: Decimal,
    ema9_last_completed: Decimal | None,
    *,
    stage_two: bool,
    rules: TickRules = ALPACA_EQUITY_TICK_RULES,
) -> Decimal | None:
    """The single resting-stop level equivalent to the current rules (``None`` = no valid stop).

    Stage 1: the initial stop.  Stage 2: ``max(breakeven, EMA9 level)`` - the breakeven part never
    moves down; only the EMA9 part (which exists solely above breakeven) can ease back toward it.
    """
    if not stage_two:
        return initial_stop(fill_price, rules)
    breakeven = breakeven_stop(fill_price, rules)
    if breakeven is None:
        return None
    if ema9_last_completed is None:
        return breakeven
    return max(breakeven, ema9_stop_level(ema9_last_completed, rules))
