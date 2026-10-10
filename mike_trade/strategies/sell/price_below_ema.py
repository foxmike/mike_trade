"""Price Below EMA SELL.

* Strategy: Price Below EMA
* Identifier: ``price_below_ema`` (v1.0.0)
* Category / sub-category: ``technical`` / ``moving_average``
* Role: SELL (advisory full exit of an owned long only)
* Source: StockCharts ChartSchool, 'Moving Averages - Simple and Exponential' (SMA/EMA formulas;
  price crossover: bearish when price moves below the average; two-moving-average
  crossover: bullish when the shorter average crosses above the longer, bearish when it
  crosses below).

Calculation (completed 1-minute bars, oldest first)::

    EMA(n)[t] = (2 / (n + 1)) * close[t] + (1 - 2 / (n + 1)) * EMA(n)[t-1]  (SMA-seeded)

Signal (level, evaluated on the latest completed bar `t`)::

    close[t] < EMA(n)[t]   ->  SELL   (level condition, not an edge)

Parameters: `period` (default 20). Lookback: `period` completed bars (20 by default); the
whole supplied history seeds the EMA. Interval: completed 1-minute bars (trigger and only
required timeframe; the one interval served to both the historical replay and the SHADOW
stream).

Interpretation decisions: A level (state) rule, so it signals on every completed bar that
closes below the EMA while a position is held; a deterministic per-bar signal id keeps
replays idempotent. StockCharts describes price moving below an MA as the bearish signal but
gives no single period; 20 is used.

Indicators run over the completed-bar history supplied by the context, which may span
sessions like a continuous chart. Regular session only; stale or non-regular-session
contexts never signal. Exit signal only: the composition layer owns position checks, minimum
hold, arbitration and full-remainder sizing; no order is submitted here. No additional
confirmation condition is applied.
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
    finish,
    gate,
    insufficient,
)
from mike_trade.strategies.indicators.core import ema
from mike_trade.strategies.sell.base import SellRule

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "DEFAULT_PERIOD",
    "WARMUP_BARS",
    "PriceBelowEmaSell",
]

STRATEGY_ID = "price_below_ema"
STRATEGY_VERSION = "1.0.0"

DEFAULT_PERIOD = 20
#: Floor of the history requirement over the allowed parameter range; the replay and the
#: SELL contract gate on this static value, so the requirement of the configured periods
#: (20 completed bars at the defaults) is enforced dynamically in the rule.
WARMUP_BARS = 2

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
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
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
        "Long exits only (full remainder); regular session only; completed 1-minute bars only.",
    ),
    reason_codes=REASON_CODES,
)


class PriceBelowEmaSell(SellRule):
    """SELL while the latest completed close is below the EMA of closes (level condition)."""

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
        bars = closed_bars(context, period)
        if bars is None:
            return insufficient(self.spec, context)

        values = ema([bar.close for bar in bars], period)
        average = values[-1]
        if average is None:
            return insufficient(self.spec, context)
        triggered = bars[-1].close < average
        return finish(self.spec, context, bars[-1], state, triggered)
