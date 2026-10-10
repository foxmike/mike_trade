"""MACD Bearish Crossover SELL.

* Strategy: MACD Bearish Crossover
* Identifier: ``macd_bearish_crossover`` (v1.0.0)
* Category / sub-category: ``technical`` / ``momentum``
* Role: SELL (advisory full exit of an owned long only)
* Source: Gerald Appel's MACD as documented in StockCharts ChartSchool, 'MACD (Moving Average
  Convergence/Divergence Oscillator)' (MACD = 12-EMA - 26-EMA, signal = 9-EMA of MACD;
  signal-line and centerline crossovers).

Calculation (completed 1-minute bars, oldest first)::

    MACD[t]   = EMA(fast)[t] - EMA(slow)[t]            (closes; EMAs SMA-seeded)
    Signal[t] = EMA(signal) of MACD, SMA-seeded over its first `signal` values

Signal (edge, evaluated once per completed bar `t` with prior bar `t-1`)::

    MACD[t-1] >= Signal[t-1]  AND  MACD[t] < Signal[t]   ->  SELL   (bearish crossover)

Parameters: `fast_period` (default 12), `slow_period` (default 26), `signal_period` (default
9); fast_period < slow_period. Lookback: `slow_period + signal_period` completed bars (35 by
default). Interval: completed 1-minute bars (trigger and only required timeframe; the one
interval served to both the historical replay and the SHADOW stream).

Interpretation decisions: StockCharts does not state the EMA seeding; the SMA seed shared
with the EMA indicator is used for all three EMAs.

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
    crossed_below,
    finish,
    gate,
    insufficient,
    known,
    require_param,
    same_session,
)
from mike_trade.strategies.indicators.core import macd
from mike_trade.strategies.sell.base import SellRule

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "DEFAULT_FAST_PERIOD",
    "DEFAULT_SLOW_PERIOD",
    "DEFAULT_SIGNAL_PERIOD",
    "WARMUP_BARS",
    "MacdBearishCrossoverSell",
]

STRATEGY_ID = "macd_bearish_crossover"
STRATEGY_VERSION = "1.0.0"

DEFAULT_FAST_PERIOD = 12
DEFAULT_SLOW_PERIOD = 26
DEFAULT_SIGNAL_PERIOD = 9
#: Floor of the history requirement over the allowed parameter range; the replay and the
#: SELL contract gate on this static value, so the requirement of the configured periods
#: (35 completed bars at the defaults) is enforced dynamically in the rule.
WARMUP_BARS = 5

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.SELL,
    category="technical",
    sub_category="momentum",
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
                min_value=2, max_value=100,
                description=(
                    "Fast EMA length of closes. Default "
                    f"{DEFAULT_FAST_PERIOD}."
                ),
            ),
            ParameterSpec(
                "slow_period", ParameterKind.INT, default=DEFAULT_SLOW_PERIOD,
                min_value=3, max_value=200,
                description=(
                    "Slow EMA length of closes; must exceed fast_period. Default "
                    f"{DEFAULT_SLOW_PERIOD}."
                ),
            ),
            ParameterSpec(
                "signal_period", ParameterKind.INT, default=DEFAULT_SIGNAL_PERIOD,
                min_value=2, max_value=100,
                description=(
                    "EMA length of the MACD line (signal line). Default "
                    f"{DEFAULT_SIGNAL_PERIOD}."
                ),
            ),
        )
    ),
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=(
        "SELL when the MACD line crosses below its signal line (bearish signal-line "
        "crossover) on a completed bar. Restates a published textbook definition; no "
        "alpha or profitability claim."
    ),
    source=(
        "Gerald Appel's MACD as documented in StockCharts ChartSchool, 'MACD (Moving "
        "Average Convergence/Divergence Oscillator)' (MACD = 12-EMA - 26-EMA, signal = "
        "9-EMA of MACD; signal-line and centerline crossovers). Not backtested here."
    ),
    execution_limitations=(
        "Advisory exit signal only: no order submission; minimum hold, arbitration and "
        "position checks belong to the composition layer.",
        "Long exits only (full remainder); regular session only; completed 1-minute bars only.",
        "The prior and latest bars of the crossing must share a New York trading date.",
    ),
    reason_codes=REASON_CODES,
)


class MacdBearishCrossoverSell(SellRule):
    """SELL when the MACD line crosses below its signal line (bearish signal-line crossover)
    on a completed bar."""

    spec = _SPEC

    def evaluate_trigger(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        validated = self.spec.parameter_schema.validate(params)
        fast_period = validated["fast_period"]
        slow_period = validated["slow_period"]
        signal_period = validated["signal_period"]
        assert isinstance(fast_period, int)
        assert isinstance(slow_period, int)
        assert isinstance(signal_period, int)
        require_param(fast_period < slow_period, "fast_period < slow_period", "fast_period")

        early = gate(self.spec, context, state)
        if early is not None:
            return early
        bars = closed_bars(context, slow_period + signal_period)
        if bars is None or not same_session(bars[-2], bars[-1]):
            return insufficient(self.spec, context)

        closes = [bar.close for bar in bars]
        macd_line, signal_line, _ = macd(closes, fast_period, slow_period, signal_period)
        if not known(macd_line[-2], macd_line[-1], signal_line[-2], signal_line[-1]):
            return insufficient(self.spec, context)
        triggered = crossed_below(macd_line[-2], signal_line[-2], macd_line[-1], signal_line[-1])
        return finish(self.spec, context, bars[-1], state, triggered)
