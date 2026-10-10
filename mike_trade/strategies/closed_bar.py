"""Plain helper functions shared by the closed-bar technical strategies.

These are stateless functions, not a framework: every strategy remains a complete
class that computes its own indicators and states its own signal condition.  The
helpers only de-duplicate the guards every closed-bar rule must apply identically:

* **Timeframe comes from the strategy's own ``StrategySpec``** (``trigger_timeframe``);
  nothing here assumes a particular interval.
* stale data, non-regular sessions and (BUY only) the engine-owned ``eligibility``
  key are rejected before any signal is produced;
* crossing comparisons use one inclusive/strict convention (prior at-or-on the
  reference, latest strictly beyond it);
* **missing bars**: the two bars of an edge must be adjacent (``latest.start ==
  prior.end``, the same convention as ``market_data.warmup``'s gap detection and the
  existing ``rsi_reversal``), so a gap — a missing 15 minutes, an overnight close —
  is never read as one continuous observation and nothing is fabricated to fill it;
* **recursive indicators** (EMA, MACD, RSI, DMI, Parabolic SAR) are resumed from a
  JSON-safe snapshot kept in the strategy's own persisted state (:func:`advance`), so
  their value never depends on how many candles a bounded history window happens to
  hold;
* replaying the same completed bar never emits twice (``last_signal_bar``).

``MarketContext.candles`` is already restricted to CLOSED, ascending, same-symbol,
same-timeframe candles whose end is not after ``as_of`` and every ``Candle`` validates
its own OHLC, so no bar is ever read before it completed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import Candle
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
    "ELIGIBILITY_KEY",
    "ELIGIBILITY_WATCHING_BUY",
    "STATE_LAST_SIGNAL_BAR",
    "STATE_INDICATOR",
    "REASON_CODES",
    "Advanced",
    "gate",
    "blocked",
    "closed_bars",
    "insufficient",
    "adjacent",
    "advance",
    "parts",
    "known",
    "difference",
    "crossed_above",
    "crossed_below",
    "require_param",
    "finish",
]

#: Engine-owned key; BUY strategies only read it.
ELIGIBILITY_KEY = "eligibility"
ELIGIBILITY_WATCHING_BUY = "WATCHING_BUY"
#: Strategy-owned key: ISO start of the bar that last fired.
STATE_LAST_SIGNAL_BAR = "last_signal_bar"
#: Strategy-owned key: snapshot of the recursive indicator state (see :func:`advance`).
STATE_INDICATOR = "indicator_state"

_STATE_VERSION = 1

REASON_CODES: tuple[ReasonCode, ...] = (
    ReasonCode.SIGNAL,
    ReasonCode.NO_SIGNAL,
    ReasonCode.INSUFFICIENT_DATA,
    ReasonCode.STALE_DATA,
    ReasonCode.NOT_ELIGIBLE,
)


# ------------------------------------------------------------------------- guards


def gate(
    spec: StrategySpec, context: MarketContext, state: Mapping[str, object] | None = None
) -> ReasonCode | None:
    """The reason the context may not produce a signal, or ``None`` when it may."""
    if context.freshness.stale:
        return ReasonCode.STALE_DATA
    if context.session_status is not SessionStatus.REGULAR:
        return ReasonCode.NOT_ELIGIBLE
    if spec.side is StrategyRole.BUY and (
        not isinstance(state, Mapping)
        or state.get(ELIGIBILITY_KEY) != ELIGIBILITY_WATCHING_BUY
    ):
        return ReasonCode.NOT_ELIGIBLE
    return None


def blocked(
    spec: StrategySpec,
    context: MarketContext,
    reason: ReasonCode,
    updates: Mapping[str, object] | None = None,
) -> StrategyEvaluation:
    """NO_SIGNAL for a gated context; ``updates`` still advances the indicator state."""
    return StrategyEvaluation.no_signal(
        spec, context.symbol, (reason,), state_updates=updates
    )


def closed_bars(
    spec: StrategySpec, context: MarketContext, minimum: int
) -> tuple[Candle, ...] | None:
    """The completed bars on the spec's trigger timeframe; ``None`` if fewer than ``minimum``."""
    bars = context.candles.get(spec.trigger_timeframe, ())
    return bars if len(bars) >= max(1, minimum) else None


def insufficient(
    spec: StrategySpec, context: MarketContext, updates: Mapping[str, object] | None = None
) -> StrategyEvaluation:
    """INSUFFICIENT_DATA: not enough history, a gap, or an indicator value is unavailable."""
    return StrategyEvaluation.no_signal(
        spec, context.symbol, (ReasonCode.INSUFFICIENT_DATA,), state_updates=updates
    )


def adjacent(prior: Candle, latest: Candle) -> bool:
    """True when ``latest`` starts exactly where ``prior`` ended (no missing bar between)."""
    return latest.start == prior.end


def known(*values: object) -> bool:
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
    updates: Mapping[str, object] | None = None,
) -> StrategyEvaluation:
    """SIGNAL for the latest completed bar when ``triggered``, else NO_SIGNAL.

    The same completed bar never emits twice: a bar already recorded in
    ``last_signal_bar`` yields NO_SIGNAL even if the condition still holds.  ``updates``
    carries the advanced indicator state into either result.
    """
    bar_iso = latest.start.isoformat()
    already = isinstance(state, Mapping) and state.get(STATE_LAST_SIGNAL_BAR) == bar_iso
    if triggered and not already:
        return StrategyEvaluation.for_signal(
            spec,
            context.symbol,
            latest.start,
            reference_price=latest.close,
            state_updates={**(updates or {}), STATE_LAST_SIGNAL_BAR: bar_iso},
        )
    return StrategyEvaluation.no_signal(
        spec, context.symbol, (ReasonCode.NO_SIGNAL,), bar_time=latest.start,
        state_updates=updates,
    )


# ------------------------------------------------------------- recursive indicators


@dataclass(frozen=True, slots=True)
class Advanced:
    """Indicator outputs on the bar before the latest and on the latest completed bar.

    Each is whatever the calculator's ``update`` returns (``None`` before the first bar).
    ``updates`` is the ``state_updates`` mapping that persists the advanced calculator.
    """

    prior: Any
    latest: Any
    updates: Mapping[str, object]


def _encode(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (tuple, list)):
        return [_encode(item) for item in value]
    raise TypeError(f"unsupported indicator output {type(value).__name__}")


def _decode(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return Decimal(value)
    if isinstance(value, list):
        return tuple(_decode(item) for item in value)
    raise TypeError(f"unsupported stored indicator output {type(value).__name__}")


def parts(value: Any, width: int) -> tuple[Any, ...]:
    """Unpack a calculator output of ``width`` elements; ``None`` (no output yet) becomes Nones."""
    return (None,) * width if value is None else tuple(value)


def advance(
    state: Mapping[str, object],
    bars: tuple[Candle, ...],
    signature: str,
    fresh: Callable[[], Any],
    restore: Callable[[Mapping[str, object]], Any],
    feed: Callable[[Any, Candle], Any],
) -> Advanced:
    """Bring a recursive indicator up to the latest completed bar, resuming saved state.

    The calculator's snapshot is kept under :data:`STATE_INDICATOR` in the strategy's own
    state together with the last processed bar, the outputs on that bar and on the bar
    before it, and ``signature`` (strategy, version and the parameters that shape the
    calculation).  On each evaluation:

    * saved state for the same bar -> its stored outputs are returned unchanged (a replay);
    * saved state for an earlier bar that the supplied history still reaches -> only the
      bars after it are fed, so the result is identical to having processed every bar in
      order, however far the bounded history window has since advanced;
    * no usable saved state (first evaluation, changed parameters, a gap the history no
      longer covers, a corrupt snapshot) -> the calculator is seeded from the supplied
      history, the published seed convention applied at the oldest available bar.

    Nothing is fabricated: only bars present in ``bars`` are ever fed.
    """
    latest = bars[-1]
    saved = state.get(STATE_INDICATOR) if isinstance(state, Mapping) else None
    calc: Any = None
    prior: Any = None
    current: Any = None
    pending: tuple[Candle, ...] = bars
    try:
        if (
            isinstance(saved, Mapping)
            and saved.get("version") == _STATE_VERSION
            and saved.get("signature") == signature
        ):
            last_start = datetime.fromisoformat(str(saved["last_bar"]))
            if bars[0].start <= last_start <= latest.start:
                if last_start == latest.start:
                    return Advanced(_decode(saved["prior"]), _decode(saved["latest"]), {})
                calc = restore(saved["calc"])  # type: ignore[arg-type]
                prior, current = _decode(saved["prior"]), _decode(saved["latest"])
                pending = tuple(bar for bar in bars if bar.start > last_start)
    except (KeyError, TypeError, ValueError, InvalidOperation):
        calc, prior, current, pending = None, None, None, bars
    if calc is None:
        calc, prior, current, pending = fresh(), None, None, bars
    for bar in pending:
        prior, current = current, feed(calc, bar)
    snapshot = {
        "version": _STATE_VERSION,
        "signature": signature,
        "last_bar": latest.start.isoformat(),
        "calc": calc.snapshot(),
        "prior": _encode(prior),
        "latest": _encode(current),
    }
    return Advanced(prior, current, {STATE_INDICATOR: snapshot})
