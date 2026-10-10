"""Inside Bar Breakout BUY.

* Strategy: Inside Bar Breakout
* Identifier: ``inside_bar_breakout`` (v1.0.0)
* Category / sub-category: ``technical`` / ``price_action``
* Role: BUY (long entry signal only)
* Source: Inside-bar breakout as described in published trading documentation (TradingView 'Inside
  Bar Breakout' scripts and LuxAlgo 'Inside Bar Breakouts'): an inside bar has a lower
  high and higher low than the mother bar; a long breaks above the mother bar's high.
  Weakly standardized: no single authoritative rule exists.

Calculation (completed 1-minute bars, oldest first)::

    mother = bar[t-2], inside = bar[t-1], breakout = bar[t]
    inside bar: inside.high < mother.high AND inside.low > mother.low

Signal (edge, evaluated once per completed bar `t` with prior bar `t-1`)::

    inside bar at t-1  AND  close[t] > mother.high   ->  BUY

Parameters: none. Lookback: 3 completed bars. Interval: completed 1-minute bars (trigger and
only required timeframe; the one interval served to both the historical replay and the
SHADOW stream).

Interpretation decisions: Only the first bar after the inside bar can break out (no multi-
bar inside sequences), the breakout is a completed close above the mother bar's high (not an
intraday touch), and the three bars must share a New York trading date. The 'body-only'
inside-bar variant is not used.

Indicators run over the completed-bar history supplied by the context, which may span
sessions like a continuous chart. The crossing itself must occur between two bars of the
same New York trading date, so an overnight gap never signals. Regular session only; stale
or non-regular-session contexts never signal. Entry signal only: no stops, sizing or orders
(protective stops belong to the existing stop policy). No additional confirmation condition
is applied.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
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
    TIMEFRAME,
    closed_bars,
    finish,
    gate,
    insufficient,
    same_session,
)

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "WARMUP_BARS",
    "InsideBarBreakoutBuy",
]

STRATEGY_ID = "inside_bar_breakout"
STRATEGY_VERSION = "1.0.0"

#: Floor of the history requirement over the allowed parameter range; the replay and the
#: SELL contract gate on this static value, so the requirement of the configured periods
#: (3 completed bars at the defaults) is enforced dynamically in the rule.
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
        "Long entries only; regular session only; completed 1-minute bars only.",
        "The prior and latest bars of the crossing must share a New York trading date.",
    ),
    reason_codes=REASON_CODES,
)


class InsideBarBreakoutBuy(BuyStrategy):
    """BUY when a completed bar closes above the high of the mother bar immediately after an
    inside bar (lower high and higher low than the mother bar)."""

    spec = _SPEC

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        self.spec.parameter_schema.validate(params)

        early = gate(self.spec, context, state)
        if early is not None:
            return early
        bars = closed_bars(context, 3)
        if bars is None or not same_session(bars[-2], bars[-1]):
            return insufficient(self.spec, context)

        mother, inside, breakout = bars[-3], bars[-2], bars[-1]
        if not same_session(mother, breakout):
            return insufficient(self.spec, context)
        is_inside = inside.high < mother.high and inside.low > mother.low
        triggered = is_inside and breakout.close > mother.high
        return finish(self.spec, context, bars[-1], state, triggered)
