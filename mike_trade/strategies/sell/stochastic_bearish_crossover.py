"""Stochastic Bearish Crossover SELL.

* Strategy: Stochastic Bearish Crossover
* Identifier: ``stochastic_bearish_crossover`` (v1.0.0)
* Category / sub-category: ``technical`` / ``oscillator``
* Role: SELL (advisory full exit of an owned long only)
* Source: George Lane's Stochastic Oscillator as documented in StockCharts ChartSchool,
  'Stochastic Oscillator' (Full Stochastic 14,3,3; %K/%D signal-line cross).

Calculation (completed 1-minute bars, oldest first)::

    raw %K = 100 * (close - lowest low(n)) / (highest high(n) - lowest low(n))
    %K = SMA(raw %K, k_smooth)        %D = SMA(%K, d_period)     (Full Stochastic 14,3,3)

Signal (edge, evaluated once per completed bar `t` with prior bar `t-1`)::

    %K[t-1] >= %D[t-1]  AND  %K[t] < %D[t]   ->  SELL   (bearish signal-line cross)

Parameters: `k_period` (default 14), `k_smooth` (default 3), `d_period` (default 3).
Lookback: `k_period + k_smooth + d_period - 1` completed bars (19 by default). Interval:
completed 1-minute bars (trigger and only required timeframe; the one interval served to
both the historical replay and the SHADOW stream).

Interpretation decisions: No overbought/oversold qualification is applied (StockCharts
defines the cross itself as the signal). A flat range leaves %K undefined and suppresses the
signal.

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
    same_session,
)
from mike_trade.strategies.indicators.core import stochastic
from mike_trade.strategies.sell.base import SellRule

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "DEFAULT_K_PERIOD",
    "DEFAULT_K_SMOOTH",
    "DEFAULT_D_PERIOD",
    "WARMUP_BARS",
    "StochasticBearishCrossoverSell",
]

STRATEGY_ID = "stochastic_bearish_crossover"
STRATEGY_VERSION = "1.0.0"

DEFAULT_K_PERIOD = 14
DEFAULT_K_SMOOTH = 3
DEFAULT_D_PERIOD = 3
#: Floor of the history requirement over the allowed parameter range; the replay and the
#: SELL contract gate on this static value, so the requirement of the configured periods
#: (19 completed bars at the defaults) is enforced dynamically in the rule.
WARMUP_BARS = 3

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.SELL,
    category="technical",
    sub_category="oscillator",
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
                "k_period", ParameterKind.INT, default=DEFAULT_K_PERIOD,
                min_value=2, max_value=100,
                description=(
                    "Raw %K look-back in completed bars. Default "
                    f"{DEFAULT_K_PERIOD}."
                ),
            ),
            ParameterSpec(
                "k_smooth", ParameterKind.INT, default=DEFAULT_K_SMOOTH,
                min_value=1, max_value=20,
                description=(
                    "SMA smoothing of raw %K (1 = Fast Stochastic). Default "
                    f"{DEFAULT_K_SMOOTH}."
                ),
            ),
            ParameterSpec(
                "d_period", ParameterKind.INT, default=DEFAULT_D_PERIOD,
                min_value=1, max_value=20,
                description=(
                    "SMA length of %D. Default "
                    f"{DEFAULT_D_PERIOD}."
                ),
            ),
        )
    ),
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=(
        "SELL when Full Stochastic %K crosses below %D on a completed bar (prior %K >= %D "
        "and latest %K < %D). Restates a published textbook definition; no alpha or "
        "profitability claim."
    ),
    source=(
        "George Lane's Stochastic Oscillator as documented in StockCharts ChartSchool, "
        "'Stochastic Oscillator' (Full Stochastic 14,3,3; %K/%D signal-line cross). Not "
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


class StochasticBearishCrossoverSell(SellRule):
    """SELL when Full Stochastic %K crosses below %D on a completed bar (prior %K >= %D and
    latest %K < %D)."""

    spec = _SPEC

    def evaluate_trigger(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        validated = self.spec.parameter_schema.validate(params)
        k_period = validated["k_period"]
        k_smooth = validated["k_smooth"]
        d_period = validated["d_period"]
        assert isinstance(k_period, int)
        assert isinstance(k_smooth, int)
        assert isinstance(d_period, int)
        need = k_period + k_smooth + d_period - 1

        early = gate(self.spec, context, state)
        if early is not None:
            return early
        bars = closed_bars(context, need)
        if bars is None or not same_session(bars[-2], bars[-1]):
            return insufficient(self.spec, context)

        window = bars[-need:]
        k, d = stochastic(window, k_period, k_smooth, d_period)
        if not known(k[-2], k[-1], d[-2], d[-1]):
            return insufficient(self.spec, context)
        triggered = crossed_below(k[-2], d[-2], k[-1], d[-1])
        return finish(self.spec, context, bars[-1], state, triggered)
