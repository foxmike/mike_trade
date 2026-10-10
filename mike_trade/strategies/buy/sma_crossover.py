"""SMA Crossover BUY.

* Strategy: SMA Crossover
* Identifier: ``sma_crossover`` (v1.0.0)
* Category / sub-category: ``technical`` / ``moving_average``
* Role: BUY (long entry signal only)
* Source: StockCharts ChartSchool, 'Moving Averages - Simple and Exponential' (SMA/EMA formulas;
  price crossover: bearish when price moves below the average; two-moving-average
  crossover: bullish when the shorter average crosses above the longer, bearish when it
  crosses below).

Calculation (completed bars, oldest first)::

    SMA(n)[t] = (close[t-n+1] + ... + close[t]) / n

Signal (edge, evaluated once per completed bar `t` with prior bar `t-1`)::

    SMA(fast)[t-1] <= SMA(slow)[t-1]  AND  SMA(fast)[t] > SMA(slow)[t]   ->  BUY

Parameters: `fast_period` (default 50), `slow_period` (default 200); fast_period <
slow_period. Lookback: `slow_period + 1` completed bars (201 by default);
`required_bars(params)` returns the requirement of the configured periods. Interval:
completed 1-minute bars (the declared `trigger_timeframe`; the one interval served to both
the historical replay and the SHADOW stream).

Interpretation decisions: The averages are the conventional 50/200 SMA (golden cross)
applied to 1-minute closes.

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
    require_param,
)
from mike_trade.strategies.indicators.core import sma

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "TIMEFRAME",
    "DEFAULT_FAST_PERIOD",
    "DEFAULT_SLOW_PERIOD",
    "WARMUP_BARS",
    "SmaCrossoverBuy",
]

STRATEGY_ID = "sma_crossover"
STRATEGY_VERSION = "1.0.0"

#: The strategy's declared trigger and only required timeframe (see `_SPEC`).
TIMEFRAME = CandleTimeframe.M1

DEFAULT_FAST_PERIOD = 50
DEFAULT_SLOW_PERIOD = 200


def _required_bars(validated: Mapping[str, object]) -> int:
    slow_period = validated["slow_period"]
    assert isinstance(slow_period, int)
    return slow_period + 1


#: Completed bars needed at the default parameters; `required_bars(params)` gives
#: the exact requirement of a configured lookback (valid shorter periods stay valid).
WARMUP_BARS = 201

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.BUY,
    category="technical",
    sub_category="moving_average",
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
                "fast_period", ParameterKind.INT, default=DEFAULT_FAST_PERIOD,
                min_value=2, max_value=200,
                description=(
                    "Fast SMA length in completed bars. Default "
                    f"{DEFAULT_FAST_PERIOD}."
                ),
            ),
            ParameterSpec(
                "slow_period", ParameterKind.INT, default=DEFAULT_SLOW_PERIOD,
                min_value=3, max_value=400,
                description=(
                    "Slow SMA length in completed bars; must exceed fast_period. Default "
                    f"{DEFAULT_SLOW_PERIOD}."
                ),
            ),
        )
    ),
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=(
        "BUY on the completed bar where the fast SMA of closes crosses above the slow SMA "
        "(prior fast <= prior slow and latest fast > latest slow). Restates a published "
        "textbook definition; no alpha or profitability claim."
    ),
    source=(
        "StockCharts ChartSchool, 'Moving Averages - Simple and Exponential' (SMA/EMA "
        "formulas; price crossover: bearish when price moves below the average; two- "
        "moving-average crossover: bullish when the shorter average crosses above the "
        "longer, bearish when it crosses below). Not backtested here."
    ),
    execution_limitations=(
        "Entry signal only: no sizing, stops, targets, or order submission.",
        "Long entries only; regular session only; completed bars only.",
        "The prior and latest bars of a signal must be adjacent (no missing bar).",
    ),
    reason_codes=REASON_CODES,
)


class SmaCrossoverBuy(BuyStrategy):
    """BUY on the completed bar where the fast SMA of closes crosses above the slow SMA (prior
    fast <= prior slow and latest fast > latest slow)."""

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
        validated = self.spec.parameter_schema.validate(params)
        fast_period = validated["fast_period"]
        slow_period = validated["slow_period"]
        assert isinstance(fast_period, int)
        assert isinstance(slow_period, int)
        require_param(fast_period < slow_period, "fast_period < slow_period", "fast_period")
        need = _required_bars(validated)

        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, need)
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context)

        slow_closes = [bar.close for bar in bars[-(slow_period + 1):]]
        fast = sma(slow_closes[-(fast_period + 1):], fast_period)
        slow = sma(slow_closes, slow_period)
        if not known(fast[-2], fast[-1], slow[-2], slow[-1]):
            return insufficient(self.spec, context)
        triggered = crossed_above(fast[-2], slow[-2], fast[-1], slow[-1])
        return finish(self.spec, context, bars[-1], state, triggered)
