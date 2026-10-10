"""Price Below SMA SELL.

* Strategy: Price Below SMA
* Identifier: ``price_below_sma`` (v1.0.0)
* Category / sub-category: ``technical`` / ``moving_average``
* Role: SELL (advisory full exit of an owned long only)
* Source: StockCharts ChartSchool, 'Moving Averages - Simple and Exponential' (SMA/EMA formulas;
  price crossover: bearish when price moves below the average; two-moving-average
  crossover: bullish when the shorter average crosses above the longer, bearish when it
  crosses below).

Calculation (completed bars, oldest first)::

    SMA(n)[t] = (close[t-n+1] + ... + close[t]) / n

Signal (level, evaluated on the latest completed bar `t`)::

    close[t] < SMA(n)[t]   ->  SELL   (level condition, not an edge)

Parameters: `period` (default 50). Lookback: `period` completed bars (50 by default);
`required_bars(params)` returns the requirement of the configured periods. Interval:
completed 1-minute bars (the declared `trigger_timeframe`; the one interval served to both
the historical replay and the SHADOW stream).

Interpretation decisions: A level (state) rule, so it signals on every completed bar that
closes below the SMA while a position is held; a deterministic per-bar signal id keeps
replays idempotent. StockCharts describes price moving below an MA as the bearish signal but
gives no single period; 50 (its medium-term example) is used.

Indicators run over the observed completed bars supplied by the context (a missing bar is
neither filled nor counted), which may span sessions like a continuous chart. Windowed
values are computed from the latest completed bars in the context. Regular session only;
stale or non-regular-session contexts never signal. Exit signal only: the composition layer
owns position checks, minimum hold, arbitration and full-remainder sizing; no order is
submitted here. No additional confirmation condition is applied.
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
    blocked,
    closed_bars,
    finish,
    gate,
    insufficient,
)
from mike_trade.strategies.indicators.core import sma
from mike_trade.strategies.sell.base import SellRule

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "TIMEFRAME",
    "DEFAULT_PERIOD",
    "WARMUP_BARS",
    "PriceBelowSmaSell",
]

STRATEGY_ID = "price_below_sma"
STRATEGY_VERSION = "1.0.0"

#: The strategy's declared trigger and only required timeframe (see `_SPEC`).
TIMEFRAME = CandleTimeframe.M1

DEFAULT_PERIOD = 50


def _required_bars(validated: Mapping[str, object]) -> int:
    period = validated["period"]
    assert isinstance(period, int)
    return period


#: Completed bars needed at the default parameters; `required_bars(params)` gives
#: the exact requirement of a configured lookback (valid shorter periods stay valid).
WARMUP_BARS = 50

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.SELL,
    category="technical",
    sub_category="moving_average",
    reference_symbols=(),
    required_timeframes=(TIMEFRAME,),
    warmup_bars=WARMUP_BARS,
    completed_bars_only=True,
    requires_volume=False,
    trigger_kind=TriggerKind.LEVEL_ON_CLOSED_BAR,
    trigger_timeframe=TIMEFRAME,
    parameter_schema=ParameterSchema(
        (
            ParameterSpec(
                "period", ParameterKind.INT, default=DEFAULT_PERIOD,
                min_value=2, max_value=400,
                description=(
                    "SMA length in completed bars. Default "
                    f"{DEFAULT_PERIOD}."
                ),
            ),
        )
    ),
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=(
        "SELL while the latest completed close is below the SMA of closes (level "
        "condition). Restates a published textbook definition; no alpha or profitability "
        "claim."
    ),
    source=(
        "StockCharts ChartSchool, 'Moving Averages - Simple and Exponential' (SMA/EMA "
        "formulas; price crossover: bearish when price moves below the average; two- "
        "moving-average crossover: bullish when the shorter average crosses above the "
        "longer, bearish when it crosses below). Not backtested here."
    ),
    execution_limitations=(
        "Advisory exit signal only: no order submission; minimum hold, arbitration and "
        "position checks belong to the composition layer.",
        "Long exits only (full remainder); regular session only; completed bars only.",
    ),
    reason_codes=REASON_CODES,
)


class PriceBelowSmaSell(SellRule):
    """SELL while the latest completed close is below the SMA of closes (level condition)."""

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

        values = sma([bar.close for bar in bars[-period:]], period)
        average = values[-1]
        if average is None:
            return insufficient(self.spec, context)
        triggered = bars[-1].close < average
        return finish(self.spec, context, bars[-1], state, triggered)
