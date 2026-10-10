"""DMI Bearish Crossover SELL.

* Strategy: DMI Bearish Crossover
* Identifier: ``dmi_bearish_crossover`` (v1.0.0)
* Category / sub-category: ``technical`` / ``trend_following``
* Role: SELL (advisory full exit of an owned long only)
* Source: J. Welles Wilder, 'New Concepts in Technical Trading Systems' (1978) as documented in
  StockCharts ChartSchool, 'Average Directional Index (ADX)' (14-period Wilder
  smoothing; +DI/-DI crossover buy and sell signals).

Calculation (completed 1-minute bars, oldest first)::

    TR, +DM, -DM from the second bar; first smoothed value = sum of the first n raw values,
    then smoothed = previous - previous / n + current.
    +DI = 100 * smoothed +DM / smoothed TR      -DI = 100 * smoothed -DM / smoothed TR

Signal (edge, evaluated once per completed bar `t` with prior bar `t-1`)::

    -DI[t-1] <= +DI[t-1]  AND  -DI[t] > +DI[t]   ->  SELL   (-DI crosses above +DI)

Parameters: `period` (default 14). Lookback: `period + 2` completed bars (16 by default);
the whole supplied history smooths the values. Interval: completed 1-minute bars (trigger
and only required timeframe; the one interval served to both the historical replay and the
SHADOW stream).

Interpretation decisions: Wilder/StockCharts also require ADX above 25 (many traders use 20)
and place the initial stop at the signal bar's high. Neither is added here: the strategy is
the plain -DI/+DI crossover and stops belong to the stop policy.

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
from mike_trade.strategies.indicators.core import directional_indicators
from mike_trade.strategies.sell.base import SellRule

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "DEFAULT_PERIOD",
    "WARMUP_BARS",
    "DmiBearishCrossoverSell",
]

STRATEGY_ID = "dmi_bearish_crossover"
STRATEGY_VERSION = "1.0.0"

DEFAULT_PERIOD = 14
#: Floor of the history requirement over the allowed parameter range; the replay and the
#: SELL contract gate on this static value, so the requirement of the configured periods
#: (16 completed bars at the defaults) is enforced dynamically in the rule.
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
                min_value=2, max_value=100,
                description=(
                    "Wilder smoothing length in completed bars. Default "
                    f"{DEFAULT_PERIOD}."
                ),
            ),
        )
    ),
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=(
        "SELL when -DI crosses above +DI on a completed bar (prior -DI <= +DI and latest "
        "-DI > +DI). Restates a published textbook definition; no alpha or profitability "
        "claim."
    ),
    source=(
        "J. Welles Wilder, 'New Concepts in Technical Trading Systems' (1978) as "
        "documented in StockCharts ChartSchool, 'Average Directional Index (ADX)' "
        "(14-period Wilder smoothing; +DI/-DI crossover buy and sell signals). Not "
        "backtested here."
    ),
    execution_limitations=(
        "Advisory exit signal only: no order submission; minimum hold, arbitration and "
        "position checks belong to the composition layer.",
        "Long exits only (full remainder); regular session only; completed 1-minute bars only.",
        "The prior and latest bars of the crossing must share a New York trading date.",
    ),
    reason_codes=REASON_CODES,
)


class DmiBearishCrossoverSell(SellRule):
    """SELL when -DI crosses above +DI on a completed bar (prior -DI <= +DI and latest -DI >
    +DI)."""

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

        plus_di, minus_di = directional_indicators(bars, period)
        if not known(plus_di[-2], plus_di[-1], minus_di[-2], minus_di[-1]):
            return insufficient(self.spec, context)
        triggered = crossed_above(minus_di[-2], plus_di[-2], minus_di[-1], plus_di[-1])
        return finish(self.spec, context, bars[-1], state, triggered)
