"""COVER when Full Stochastic %K crosses above %D."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import (
    CoverStrategy,
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
from mike_trade.strategies.indicators.core import stochastic

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "cover_stochastic_bullish_crossover"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


def _required_bars(validated: Mapping[str, object]) -> int:
    k_period, k_smooth = validated["k_period"], validated["k_smooth"]
    d_period = validated["d_period"]
    assert isinstance(k_period, int) and isinstance(k_smooth, int) and isinstance(d_period, int)
    return k_period + k_smooth + d_period - 1


class CoverStochasticBullishCrossover(CoverStrategy):
    """COVER when Full Stochastic %K crosses above %D on a completed bar.

    ``raw %K = 100 * (close - lowest low(n)) / (highest high(n) - lowest low(n))``,
    ``%K = SMA(raw %K, k_smooth)``, ``%D = SMA(%K, d_period)`` (Full Stochastic 14,3,3; shared
    ``stochastic`` helper).  Signal (edge, prior bar ``t-1`` and latest completed bar ``t``)::

        %K[t-1] <= %D[t-1]  AND  %K[t] > %D[t]  ->  COVER

    The selected published condition is StockCharts' bare signal-line cross: no oversold-zone
    qualification is applied (as in the stochastic_crossover BUY), so the cross may occur at any
    level.  A flat range leaves %K undefined and suppresses the signal.  Finite window, so no
    indicator state is persisted.  Needs ``k_period + k_smooth + d_period - 1`` completed bars
    (19 by default).
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version=STRATEGY_VERSION,
        side=StrategyRole.COVER,
        category="technical",
        sub_category="oscillator",
        reference_symbols=(),
        required_timeframes=(TIMEFRAME,),
        warmup_bars=19,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec(
                    "k_period", ParameterKind.INT, 14, 2, 100,
                    description="Raw %K look-back in completed bars. Default 14.",
                ),
                ParameterSpec(
                    "k_smooth", ParameterKind.INT, 3, 1, 20,
                    description="SMA smoothing of raw %K (1 = Fast Stochastic). Default 3.",
                ),
                ParameterSpec(
                    "d_period", ParameterKind.INT, 3, 1, 20,
                    description="SMA length of %D. Default 3.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=(
            "COVER when Full Stochastic %K crosses above %D on a completed bar (prior %K <= %D "
            "and latest %K > %D). Restates a published textbook definition; no alpha or "
            "profitability claim."
        ),
        source=(
            "George Lane's Stochastic Oscillator as documented in StockCharts ChartSchool, "
            "'Stochastic Oscillator' (Full Stochastic 14,3,3; %K/%D signal-line cross). Same "
            "calculation and trigger as the stochastic_crossover BUY."
        ),
        execution_limitations=(
            "Exit signal only: the engine must evaluate it only while a managed short exists and decides the cover order, quantity and any follow-on action; it never implies a BUY.",
            "No oversold-zone filter: a %K/%D cross at a high level also signals.",
            "Completed adjacent M1 bars; regular session only; stale data never signals.",
        ),
        reason_codes=REASON_CODES,
    )

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
        k_period, k_smooth = validated["k_period"], validated["k_smooth"]
        d_period = validated["d_period"]
        assert isinstance(k_period, int) and isinstance(k_smooth, int) and isinstance(d_period, int)
        need = _required_bars(validated)

        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, need)
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context)
        k, d = stochastic(bars[-need:], k_period, k_smooth, d_period)
        if not known(k[-2], k[-1], d[-2], d[-1]):
            return insufficient(self.spec, context)
        triggered = crossed_above(k[-2], d[-2], k[-1], d[-1])
        return finish(self.spec, context, bars[-1], state, triggered)
