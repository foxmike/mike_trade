"""EMA Bearish Crossover SELL.

* Strategy: EMA Bearish Crossover
* Identifier: ``ema_bearish_crossover`` (v1.0.0)
* Category / sub-category: ``technical`` / ``moving_average``
* Role: SELL (advisory full exit of an owned long only)
* Source: StockCharts ChartSchool, 'Moving Averages - Simple and Exponential' (SMA/EMA formulas;
  price crossover: bearish when price moves below the average; two-moving-average
  crossover: bullish when the shorter average crosses above the longer, bearish when it
  crosses below).

Calculation (completed bars, oldest first)::

    EMA(n)[t] = (2 / (n + 1)) * close[t] + (1 - 2 / (n + 1)) * EMA(n)[t-1]
    EMA(n) is seeded with the SMA of the first n closes (StockCharts convention).

Signal (edge, evaluated once per completed bar `t` with prior bar `t-1`)::

    EMA(fast)[t-1] >= EMA(slow)[t-1]  AND  EMA(fast)[t] < EMA(slow)[t]   ->  SELL

Parameters: `fast_period` (default 10), `slow_period` (default 50); fast_period <
slow_period. Lookback: `slow_period + 1` completed bars (51 by default); the whole supplied
history seeds the EMA; `required_bars(params)` returns the requirement of the configured
periods. Interval: completed 1-minute bars (the declared `trigger_timeframe`; the one
interval served to both the historical replay and the SHADOW stream).

Interpretation decisions: The published page gives 10/50 (and 5/35, 50/200) as example EMA
pairs and no single default; 10/50 is used. The EMA is seeded at the start of the supplied
history, so early values are seed-dominated.

Indicators run over the observed completed bars supplied by the context (a missing bar is
neither filled nor counted), which may span sessions like a continuous chart. The prior and
latest bars of the signal must be adjacent completed bars (no missing bar between them),
otherwise the result is INSUFFICIENT_DATA. The recursive indicator state is persisted under
`indicator_state` in the strategy's own state and resumed bar by bar, so values do not
depend on the size of the candle history window; absent or unusable state is re-seeded from
the supplied history. Regular session only; stale or non-regular-session contexts never
signal. Exit signal only: the composition layer owns position checks, minimum hold,
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
    STATE_INDICATOR,
    STATE_LAST_SIGNAL_BAR,
    adjacent,
    advance,
    blocked,
    closed_bars,
    crossed_below,
    finish,
    gate,
    insufficient,
    known,
    parts,
    require_param,
)
from mike_trade.strategies.indicators.core import MACDCalculator
from mike_trade.strategies.sell.base import SellRule

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "TIMEFRAME",
    "DEFAULT_FAST_PERIOD",
    "DEFAULT_SLOW_PERIOD",
    "WARMUP_BARS",
    "EmaBearishCrossoverSell",
]

STRATEGY_ID = "ema_bearish_crossover"
STRATEGY_VERSION = "1.0.0"

#: The strategy's declared trigger and only required timeframe (see `_SPEC`).
TIMEFRAME = CandleTimeframe.M1

DEFAULT_FAST_PERIOD = 10
DEFAULT_SLOW_PERIOD = 50


def _required_bars(validated: Mapping[str, object]) -> int:
    slow_period = validated["slow_period"]
    assert isinstance(slow_period, int)
    return slow_period + 1


#: Completed bars needed at the default parameters; `required_bars(params)` gives
#: the exact requirement of a configured lookback (valid shorter periods stay valid).
WARMUP_BARS = 51

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
    trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
    trigger_timeframe=TIMEFRAME,
    parameter_schema=ParameterSchema(
        (
            ParameterSpec(
                "fast_period", ParameterKind.INT, default=DEFAULT_FAST_PERIOD,
                min_value=2, max_value=200,
                description=(
                    "Fast EMA length in completed bars. Default "
                    f"{DEFAULT_FAST_PERIOD}."
                ),
            ),
            ParameterSpec(
                "slow_period", ParameterKind.INT, default=DEFAULT_SLOW_PERIOD,
                min_value=3, max_value=400,
                description=(
                    "Slow EMA length in completed bars; must exceed fast_period. Default "
                    f"{DEFAULT_SLOW_PERIOD}."
                ),
            ),
        )
    ),
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_INDICATOR),
    methodology=(
        "SELL on the completed bar where the fast EMA of closes crosses below the slow "
        "EMA (prior fast >= prior slow and latest fast < latest slow). Restates a "
        "published textbook definition; no alpha or profitability claim."
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
        "The prior and latest bars of a signal must be adjacent (no missing bar).",
    ),
    reason_codes=REASON_CODES,
)


class EmaBearishCrossoverSell(SellRule):
    """SELL on the completed bar where the fast EMA of closes crosses below the slow EMA
    (prior fast >= prior slow and latest fast < latest slow)."""

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
        signature = f"{STRATEGY_ID}|{STRATEGY_VERSION}|{fast_period}|{slow_period}"
        adv = advance(
            state, bars, signature,
            fresh=lambda: MACDCalculator(fast_period, slow_period, None),
            restore=MACDCalculator.restore,
            feed=lambda calc, bar: calc.update(bar.close),
        )
        if reason is not None:
            return blocked(self.spec, context, reason, adv.updates)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context, adv.updates)

        fast_prior, slow_prior, *_ = parts(adv.prior, 4)
        fast_latest, slow_latest, *_ = parts(adv.latest, 4)
        if not known(fast_prior, slow_prior, fast_latest, slow_latest):
            return insufficient(self.spec, context, adv.updates)
        triggered = crossed_below(fast_prior, slow_prior, fast_latest, slow_latest)
        return finish(self.spec, context, bars[-1], state, triggered, adv.updates)
