"""Aroon Bearish Reversal SELL.

* Strategy: Aroon Bearish Reversal
* Identifier: ``aroon_bearish_reversal`` (v1.0.0)
* Category / sub-category: ``technical`` / ``trend_following``
* Role: SELL (advisory full exit of an owned long only)
* Source: Tushar Chande's Aroon (1995) as documented in StockCharts ChartSchool, 'Aroon' (25
  periods; Aroon-Up crossing above Aroon-Down is the first stage of an uptrend, the
  reverse of a downtrend).

Calculation (completed 1-minute bars, oldest first)::

    Aroon-Up   = 100 * (n - bars since highest high) / n
    Aroon-Down = 100 * (n - bars since lowest low)  / n
    (extreme searched over the latest n + 1 bars; ties resolve to the most recent bar)

Signal (edge, evaluated once per completed bar `t` with prior bar `t-1`)::

    Down[t-1] <= Up[t-1]  AND  Down[t] > Up[t]   ->  SELL   (a downtrend emerges)

Parameters: `period` (default 25). Lookback: `period + 2` completed bars (27 by default).
Interval: completed 1-minute bars (trigger and only required timeframe; the one interval
served to both the historical replay and the SHADOW stream).

Interpretation decisions: StockCharts does not define the search window or ties; the
Chande/TA-Lib convention (n + 1 bars, most recent tie) is used. The 50/70/100 strength
levels are not required.

Indicators run over the completed-bar history supplied by the context, which may span
sessions like a continuous chart. The crossing itself must occur between two bars of the
same New York trading date, so an overnight gap never signals. Regular session only; stale
or non-regular-session contexts never signal. Exit signal only: the composition layer owns
position checks, minimum hold, arbitration and full-remainder sizing; no order is submitted
here. No additional confirmation condition is applied.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.strategies.base import (
    ParameterKind,
    ParameterSchema,
    ParameterSpec,
    StrategyEvaluation,
    StrategySpec,
    TriggerKind,
)
from mike_trade.strategies.closed_bar import (
    REASON_CODES,
    STATE_LAST_SIGNAL_BAR,
    TIMEFRAME,
    closed_bars,
    crossed_above,
    finish,
    gate,
    insufficient,
    known,
    same_session,
)
from mike_trade.strategies.indicators.core import aroon
from mike_trade.strategies.sell.base import SellRule

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "DEFAULT_PERIOD",
    "WARMUP_BARS",
    "AroonBearishReversalSell",
]

STRATEGY_ID = "aroon_bearish_reversal"
STRATEGY_VERSION = "1.0.0"

DEFAULT_PERIOD = 25
#: Floor of the history requirement over the allowed parameter range; the replay and the
#: SELL contract gate on this static value, so the requirement of the configured periods
#: (27 completed bars at the defaults) is enforced dynamically in the rule.
WARMUP_BARS = 4

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.SELL,
    category="technical",
    sub_category="trend_following",
    reference_symbols=(),
    required_timeframes=(TIMEFRAME,),
    warmup_bars=WARMUP_BARS,
    completed_bars_only=True,
    requires_volume=False,
    trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
    trigger_timeframe=TIMEFRAME,
    parameter_schema=ParameterSchema(
        (
            ParameterSpec(
                "period", ParameterKind.INT, default=DEFAULT_PERIOD,
                min_value=2, max_value=200,
                description=(
                    "Aroon look-back in completed bars. Default "
                    f"{DEFAULT_PERIOD}."
                ),
            ),
        )
    ),
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=(
        "SELL when Aroon-Down crosses above Aroon-Up on a completed bar (a downtrend "
        "emerging). Restates a published textbook definition; no alpha or profitability "
        "claim."
    ),
    source=(
        "Tushar Chande's Aroon (1995) as documented in StockCharts ChartSchool, 'Aroon' "
        "(25 periods; Aroon-Up crossing above Aroon-Down is the first stage of an "
        "uptrend, the reverse of a downtrend). Not backtested here."
    ),
    execution_limitations=(
        "Advisory exit signal only: no order submission; minimum hold, arbitration and "
        "position checks belong to the composition layer.",
        "Long exits only (full remainder); regular session only; completed 1-minute bars only.",
        "The prior and latest bars of the crossing must share a New York trading date.",
    ),
    reason_codes=REASON_CODES,
)


class AroonBearishReversalSell(SellRule):
    """SELL when Aroon-Down crosses above Aroon-Up on a completed bar (a downtrend
    emerging)."""

    spec = _SPEC

    def evaluate_trigger(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        validated = self.spec.parameter_schema.validate(params)
        period = validated["period"]
        assert isinstance(period, int)

        early = gate(self.spec, context, state)
        if early is not None:
            return early
        bars = closed_bars(context, period + 2)
        if bars is None or not same_session(bars[-2], bars[-1]):
            return insufficient(self.spec, context)

        window = bars[-(period + 2):]
        up, down = aroon(window, period)
        if not known(up[-2], up[-1], down[-2], down[-1]):
            return insufficient(self.spec, context)
        triggered = crossed_above(down[-2], up[-2], down[-1], up[-1])
        return finish(self.spec, context, bars[-1], state, triggered)
