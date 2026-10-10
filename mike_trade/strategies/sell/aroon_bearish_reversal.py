"""Aroon Bearish Reversal SELL.

* Strategy: Aroon Bearish Reversal
* Identifier: ``aroon_bearish_reversal`` (v1.0.0)
* Category / sub-category: ``technical`` / ``trend_following``
* Role: SELL (advisory full exit of an owned long only)
* Source: Tushar Chande's Aroon (1995) as documented in StockCharts ChartSchool, 'Aroon' (25
  periods; Aroon-Up crossing above Aroon-Down is the first stage of an uptrend, the
  reverse of a downtrend).

Calculation (completed bars, oldest first)::

    Aroon-Up   = 100 * (n - bars since highest high) / n
    Aroon-Down = 100 * (n - bars since lowest low)  / n
    (extreme searched over the latest n + 1 bars; ties resolve to the most recent bar)

Signal (edge, evaluated once per completed bar `t` with prior bar `t-1`)::

    Down[t-1] <= Up[t-1]  AND  Down[t] > Up[t]   ->  SELL   (a downtrend emerges)

Parameters: `period` (default 25). Lookback: `period + 2` completed bars (27 by default);
`required_bars(params)` returns the requirement of the configured periods. Interval:
completed 1-minute bars (the declared `trigger_timeframe`; the one interval served to both
the historical replay and the SHADOW stream).

Interpretation decisions: StockCharts does not define the search window or ties; the
Chande/TA-Lib convention (n + 1 bars, most recent tie) is used. The 50/70/100 strength
levels are not required.

Indicators run over the observed completed bars supplied by the context (a missing bar is
neither filled nor counted), which may span sessions like a continuous chart. The prior and
latest bars of the signal must be adjacent completed bars (no missing bar between them),
otherwise the result is INSUFFICIENT_DATA. Windowed values are computed from the latest
completed bars in the context. Regular session only; stale or non-regular-session contexts
never signal. Exit signal only: the composition layer owns position checks, minimum hold,
arbitration and full-remainder sizing; no order is submitted here. No additional
confirmation condition is applied.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
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
    adjacent,
    blocked,
    closed_bars,
    crossed_above,
    finish,
    gate,
    insufficient,
    known,
)
from mike_trade.strategies.indicators.core import aroon
from mike_trade.strategies.sell.base import SellRule

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "TIMEFRAME",
    "DEFAULT_PERIOD",
    "WARMUP_BARS",
    "AroonBearishReversalSell",
]

STRATEGY_ID = "aroon_bearish_reversal"
STRATEGY_VERSION = "1.0.0"

#: The strategy's declared trigger and only required timeframe (see `_SPEC`).
TIMEFRAME = CandleTimeframe.M1

DEFAULT_PERIOD = 25


def _required_bars(validated: Mapping[str, object]) -> int:
    period = validated["period"]
    assert isinstance(period, int)
    return period + 2


#: Completed bars needed at the default parameters; `required_bars(params)` gives
#: the exact requirement of a configured lookback (valid shorter periods stay valid).
WARMUP_BARS = 27

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
        "Long exits only (full remainder); regular session only; completed bars only.",
        "The prior and latest bars of a signal must be adjacent (no missing bar).",
    ),
    reason_codes=REASON_CODES,
)


class AroonBearishReversalSell(SellRule):
    """SELL when Aroon-Down crosses above Aroon-Up on a completed bar (a downtrend
    emerging)."""

    spec = _SPEC

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        return _required_bars(cls.spec.parameter_schema.validate(params))

    def evaluate_trigger(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        validated = self.spec.parameter_schema.validate(params)
        period = validated["period"]
        assert isinstance(period, int)
        need = _required_bars(validated)

        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, need)
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context)

        window = bars[-(period + 2):]
        up, down = aroon(window, period)
        if not known(up[-2], up[-1], down[-2], down[-1]):
            return insufficient(self.spec, context)
        triggered = crossed_above(down[-2], up[-2], down[-1], up[-1])
        return finish(self.spec, context, bars[-1], state, triggered)
