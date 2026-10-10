"""Price Below EMA SELL.

* Strategy: Price Below EMA
* Identifier: ``price_below_ema`` (v1.0.0)
* Category / sub-category: ``technical`` / ``moving_average``
* Role: SELL (advisory full exit of an owned long only)
* Source: StockCharts ChartSchool, 'Moving Averages - Simple and Exponential' (SMA/EMA formulas;
  price crossover: bearish when price moves below the average; two-moving-average
  crossover: bullish when the shorter average crosses above the longer, bearish when it
  crosses below).

Calculation (completed bars, oldest first)::

    EMA(n)[t] = (2 / (n + 1)) * close[t] + (1 - 2 / (n + 1)) * EMA(n)[t-1]  (SMA-seeded)

Signal (level, evaluated on the latest completed bar `t`)::

    close[t] < EMA(n)[t]   ->  SELL   (level condition, not an edge)

Parameters: `period` (default 20). Lookback: `period` completed bars (20 by default); the
whole supplied history seeds the EMA; `required_bars(params)` returns the requirement of the
configured periods. Interval: completed 1-minute bars (the declared `trigger_timeframe`; the
one interval served to both the historical replay and the SHADOW stream).

Interpretation decisions: A level (state) rule, so it signals on every completed bar that
closes below the EMA while a position is held; a deterministic per-bar signal id keeps
replays idempotent. StockCharts describes price moving below an MA as the bearish signal but
gives no single period; 20 is used.

Indicators run over the observed completed bars supplied by the context (a missing bar is
neither filled nor counted), which may span sessions like a continuous chart. The recursive
indicator state is persisted under `indicator_state` in the strategy's own state and resumed
bar by bar, so values do not depend on the size of the candle history window; absent or
unusable state is re-seeded from the supplied history. Regular session only; stale or non-
regular-session contexts never signal. Exit signal only: the composition layer owns position
checks, minimum hold, arbitration and full-remainder sizing; no order is submitted here. No
additional confirmation condition is applied.
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
    advance,
    blocked,
    closed_bars,
    finish,
    gate,
    insufficient,
)
from mike_trade.strategies.indicators.core import EMACalculator
from mike_trade.strategies.sell.base import SellRule

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "TIMEFRAME",
    "DEFAULT_PERIOD",
    "WARMUP_BARS",
    "PriceBelowEmaSell",
]

STRATEGY_ID = "price_below_ema"
STRATEGY_VERSION = "1.0.0"

#: The strategy's declared trigger and only required timeframe (see `_SPEC`).
TIMEFRAME = CandleTimeframe.M1

DEFAULT_PERIOD = 20


def _required_bars(validated: Mapping[str, object]) -> int:
    period = validated["period"]
    assert isinstance(period, int)
    return period


#: Completed bars needed at the default parameters; `required_bars(params)` gives
#: the exact requirement of a configured lookback (valid shorter periods stay valid).
WARMUP_BARS = 20

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
                    "EMA length in completed bars. Default "
                    f"{DEFAULT_PERIOD}."
                ),
            ),
        )
    ),
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_INDICATOR),
    methodology=(
        "SELL while the latest completed close is below the EMA of closes (level "
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


class PriceBelowEmaSell(SellRule):
    """SELL while the latest completed close is below the EMA of closes (level condition)."""

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
        signature = f"{STRATEGY_ID}|{STRATEGY_VERSION}|{period}"
        adv = advance(
            state, bars, signature,
            fresh=lambda: EMACalculator(period),
            restore=EMACalculator.restore,
            feed=lambda calc, bar: calc.update(bar.close),
        )
        if reason is not None:
            return blocked(self.spec, context, reason, adv.updates)

        average = adv.latest
        if average is None:
            return insufficient(self.spec, context, adv.updates)
        triggered = bars[-1].close < average
        return finish(self.spec, context, bars[-1], state, triggered, adv.updates)
