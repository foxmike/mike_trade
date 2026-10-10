"""Plain helper functions shared by the closed-bar technical strategies.

These are stateless functions, not a framework: every strategy remains a complete
class that computes its own indicators and states its own signal condition.  The
helpers only de-duplicate the guards every closed-bar rule must apply identically:

* stale data, non-regular sessions and (BUY only) the engine-owned ``eligibility``
  key are rejected before any calculation;
* crossing comparisons use one inclusive/strict convention (prior at-or-on the
  reference, latest strictly beyond it);
* the prior and latest bars of an edge must belong to the same New York trading
  date, so an overnight gap is never read as an intraday crossing;
* replaying the same completed bar never emits twice (``last_signal_bar``).

``MarketContext.candles`` is already restricted to CLOSED, ascending, same-symbol,
same-timeframe candles whose end is not after ``as_of`` and every ``Candle``
validates its own OHLC, so no bar is ever read before it completed.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import Candle, CandleTimeframe
from mike_trade.scheduling.calendar import SessionStatus
from mike_trade.strategies.base import (
    ParameterValidationError,
    ReasonCode,
    StrategyEvaluation,
    StrategySpec,
)

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "TIMEFRAME",
    "ELIGIBILITY_KEY",
    "ELIGIBILITY_WATCHING_BUY",
    "STATE_LAST_SIGNAL_BAR",
    "REASON_CODES",
    "gate",
    "closed_bars",
    "insufficient",
    "same_session",
    "known",
    "difference",
    "crossed_above",
    "crossed_below",
    "require_param",
    "finish",
]

#: All closed-bar technical strategies trigger on completed 1-minute bars: the only
#: interval available both to the historical replay and to the SHADOW stream.
TIMEFRAME = CandleTimeframe.M1

#: Engine-owned key; BUY strategies only read it.
ELIGIBILITY_KEY = "eligibility"
ELIGIBILITY_WATCHING_BUY = "WATCHING_BUY"
#: Strategy-owned key: ISO start of the bar that last fired.
STATE_LAST_SIGNAL_BAR = "last_signal_bar"

REASON_CODES: tuple[ReasonCode, ...] = (
    ReasonCode.SIGNAL,
    ReasonCode.NO_SIGNAL,
    ReasonCode.INSUFFICIENT_DATA,
    ReasonCode.STALE_DATA,
    ReasonCode.NOT_ELIGIBLE,
)

_NEW_YORK = ZoneInfo("America/New_York")


def gate(
    spec: StrategySpec, context: MarketContext, state: Mapping[str, object] | None = None
) -> StrategyEvaluation | None:
    """Return a no-signal result when the context may not be evaluated, else ``None``."""
    symbol = context.symbol
    if context.freshness.stale:
        return StrategyEvaluation.no_signal(spec, symbol, (ReasonCode.STALE_DATA,))
    if context.session_status is not SessionStatus.REGULAR:
        return StrategyEvaluation.no_signal(spec, symbol, (ReasonCode.NOT_ELIGIBLE,))
    if spec.side is StrategyRole.BUY and (
        not isinstance(state, Mapping)
        or state.get(ELIGIBILITY_KEY) != ELIGIBILITY_WATCHING_BUY
    ):
        return StrategyEvaluation.no_signal(spec, symbol, (ReasonCode.NOT_ELIGIBLE,))
    return None


def closed_bars(context: MarketContext, minimum: int) -> tuple[Candle, ...] | None:
    """The completed 1-minute history, or ``None`` when fewer than ``minimum`` bars exist."""
    bars = context.candles.get(TIMEFRAME, ())
    return bars if len(bars) >= minimum else None


def insufficient(spec: StrategySpec, context: MarketContext) -> StrategyEvaluation:
    """INSUFFICIENT_DATA: not enough history or an indicator value is unavailable."""
    return StrategyEvaluation.no_signal(
        spec, context.symbol, (ReasonCode.INSUFFICIENT_DATA,)
    )


def same_session(prior: Candle, latest: Candle) -> bool:
    """True when both bars start on the same New York calendar date."""
    return prior.start.astimezone(_NEW_YORK).date() == latest.start.astimezone(_NEW_YORK).date()


def known(*values: Decimal | None) -> bool:
    """True when every indicator value is available (not ``None``)."""
    return all(value is not None for value in values)


def difference(first: Decimal | None, second: Decimal | None) -> Decimal | None:
    """``first - second`` or ``None`` when either operand is unavailable."""
    return None if first is None or second is None else first - second


def crossed_above(
    prior_a: Decimal | None,
    prior_b: Decimal | None,
    latest_a: Decimal | None,
    latest_b: Decimal | None,
) -> bool:
    """``a`` crosses above ``b``: ``prior_a <= prior_b`` and ``latest_a > latest_b``.

    False when any value is unavailable.
    """
    if prior_a is None or prior_b is None or latest_a is None or latest_b is None:
        return False
    return prior_a <= prior_b and latest_a > latest_b


def crossed_below(
    prior_a: Decimal | None,
    prior_b: Decimal | None,
    latest_a: Decimal | None,
    latest_b: Decimal | None,
) -> bool:
    """``a`` crosses below ``b``: ``prior_a >= prior_b`` and ``latest_a < latest_b``.

    False when any value is unavailable.
    """
    if prior_a is None or prior_b is None or latest_a is None or latest_b is None:
        return False
    return prior_a >= prior_b and latest_a < latest_b


def require_param(condition: bool, message: str, parameter: str) -> None:
    """Fail closed on a cross-field parameter constraint the per-field ranges cannot express."""
    if not condition:
        raise ParameterValidationError(message, code="out_of_range", parameter=parameter)


def finish(
    spec: StrategySpec,
    context: MarketContext,
    latest: Candle,
    state: Mapping[str, object],
    triggered: bool,
) -> StrategyEvaluation:
    """SIGNAL for the latest completed bar when ``triggered``, else NO_SIGNAL.

    The same completed bar never emits twice: a bar already recorded in
    ``last_signal_bar`` yields NO_SIGNAL even if the condition still holds.
    """
    bar_iso = latest.start.isoformat()
    already = isinstance(state, Mapping) and state.get(STATE_LAST_SIGNAL_BAR) == bar_iso
    if triggered and not already:
        return StrategyEvaluation.for_signal(
            spec,
            context.symbol,
            latest.start,
            reference_price=latest.close,
            state_updates={STATE_LAST_SIGNAL_BAR: bar_iso},
        )
    return StrategyEvaluation.no_signal(
        spec, context.symbol, (ReasonCode.NO_SIGNAL,), bar_time=latest.start
    )
