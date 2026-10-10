"""Inside Bar Breakout BUY.

* Strategy: Inside Bar Breakout
* Identifier: ``inside_bar_breakout`` (v1.0.0)
* Category / sub-category: ``technical`` / ``price_action``
* Role: BUY (long entry signal only)
* Source: Inside-bar breakout as described in published trading documentation (TradingView 'Inside
  Bar Breakout' scripts and LuxAlgo 'Inside Bar Breakouts'): an inside bar has a lower
  high and higher low than the mother bar; a long breaks above the mother bar's high.
  Weakly standardized: no single authoritative rule exists.

Calculation (completed bars, oldest first)::

    mother = bar[t-2], inside = bar[t-1], breakout = bar[t]
    inside bar: inside.high < mother.high AND inside.low > mother.low

Signal (edge, evaluated once per completed bar `t` with prior bar `t-1`)::

    inside bar at t-1  AND  close[t] > mother.high   ->  BUY

Parameters: none. Lookback: 3 completed bars; `required_bars(params)` returns the
requirement of the configured periods. Interval: completed 1-minute bars (the declared
`trigger_timeframe`; the one interval served to both the historical replay and the SHADOW
stream).

Interpretation decisions: Only the first bar after the inside bar can break out (no multi-
bar inside sequences), the breakout is a completed close above the mother bar's high (not an
intraday touch), and the three bars must be consecutive completed bars (no missing bar
between them). The 'body-only' inside-bar variant is not used.

Indicators run over the observed completed bars supplied by the context (a missing bar is
neither filled nor counted), which may span sessions like a continuous chart. The prior and
latest bars of the signal must be adjacent completed bars (no missing bar between them),
otherwise the result is INSUFFICIENT_DATA. Windowed values are computed from the latest
completed bars in the context. Regular session only; stale or non-regular-session contexts
never signal. Entry signal only: no stops, sizing or orders (protective stops belong to the
existing stop policy). No additional confirmation condition is applied.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import (
    BuyStrategy,
    ParameterSchema,
    StrategyEvaluation,
    StrategySpec,
    TriggerKind,
)
from mike_trade.strategies.closed_bar import (
    REASON_CODES,
    STATE_LAST_SIGNAL_BAR,
    adjacent,
    blocked,
    closed_bars,
    finish,
    gate,
    insufficient,
)

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "TIMEFRAME",
    "WARMUP_BARS",
    "InsideBarBreakoutBuy",
]

STRATEGY_ID = "inside_bar_breakout"
STRATEGY_VERSION = "1.0.0"

#: The strategy's declared trigger and only required timeframe (see `_SPEC`).
TIMEFRAME = CandleTimeframe.M1



def _required_bars(validated: Mapping[str, object]) -> int:  # noqa: ARG001
    return 3


#: Completed bars needed at the default parameters; `required_bars(params)` gives
#: the exact requirement of a configured lookback (valid shorter periods stay valid).
WARMUP_BARS = 3

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.BUY,
    category="technical",
    sub_category="price_action",
    reference_symbols=(),
    required_timeframes=(TIMEFRAME,),
    warmup_bars=WARMUP_BARS,
    completed_bars_only=True,
    requires_volume=False,
    trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
    trigger_timeframe=TIMEFRAME,
    parameter_schema=ParameterSchema(),
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=(
        "BUY when a completed bar closes above the high of the mother bar immediately "
        "after an inside bar (lower high and higher low than the mother bar). Restates a "
        "published textbook definition; no alpha or profitability claim."
    ),
    source=(
        "Inside-bar breakout as described in published trading documentation (TradingView "
        "'Inside Bar Breakout' scripts and LuxAlgo 'Inside Bar Breakouts'): an inside bar "
        "has a lower high and higher low than the mother bar; a long breaks above the "
        "mother bar's high. Weakly standardized: no single authoritative rule exists. Not "
        "backtested here."
    ),
    execution_limitations=(
        "Entry signal only: no sizing, stops, targets, or order submission.",
        "Long entries only; regular session only; completed bars only.",
        "The prior and latest bars of a signal must be adjacent (no missing bar).",
    ),
    reason_codes=REASON_CODES,
)


class InsideBarBreakoutBuy(BuyStrategy):
    """BUY when a completed bar closes above the high of the mother bar immediately after an
    inside bar (lower high and higher low than the mother bar)."""

    spec = _SPEC

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        return _required_bars(cls.spec.parameter_schema.validate(params))

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        self.spec.parameter_schema.validate(params)
        need = 3

        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, need)
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)

        mother, inside, breakout = bars[-3:]
        if not (adjacent(mother, inside) and adjacent(inside, breakout)):
            return insufficient(self.spec, context)
        is_inside = inside.high < mother.high and inside.low > mother.low
        triggered = is_inside and breakout.close > mother.high
        return finish(self.spec, context, bars[-1], state, triggered)
