"""COVER when the fast SMA crosses above the slow SMA."""

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
    require_param,
)
from mike_trade.strategies.indicators.core import sma

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "cover_sma_bullish_crossover"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


def _required_bars(validated: Mapping[str, object]) -> int:
    slow_period = validated["slow_period"]
    assert isinstance(slow_period, int)
    return slow_period + 1


class CoverSmaBullishCrossover(CoverStrategy):
    """COVER when SMA(`fast_period`) crosses above SMA(`slow_period`) on a completed bar.

    ``SMA[t]`` is the arithmetic mean of the latest n closes.  Signal (edge, prior bar ``t-1``
    and latest completed bar ``t``)::

        fast[t-1] <= slow[t-1]  AND  fast[t] > slow[t]  ->  COVER

    Both averages are finite windows of the supplied completed bars, so no indicator state is
    persisted.  Needs ``slow_period + 1`` completed bars (201 by default).
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version=STRATEGY_VERSION,
        side=StrategyRole.COVER,
        category="technical",
        sub_category="moving_average",
        reference_symbols=(),
        required_timeframes=(TIMEFRAME,),
        warmup_bars=201,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec(
                    "fast_period", ParameterKind.INT, 50, 2, 200,
                    description="Fast SMA length of closes. Default 50.",
                ),
                ParameterSpec(
                    "slow_period", ParameterKind.INT, 200, 3, 400,
                    description="Slow SMA length of closes; must exceed fast_period. Default 200.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=(
            "COVER when the fast SMA of closes crosses above the slow SMA on a completed bar "
            "(prior fast <= prior slow and latest fast > latest slow). Restates a published "
            "textbook definition; no alpha or profitability claim."
        ),
        source=(
            "StockCharts ChartSchool, 'Moving Averages - Simple and Exponential' (SMA formula; "
            "two-moving-average crossover: bullish when the shorter average crosses above the "
            "longer). Same calculation as the sma_crossover BUY."
        ),
        execution_limitations=(
            "Exit signal only: the engine must evaluate it only while a managed short exists and decides the cover order, quantity and any follow-on action; it never implies a BUY.",
            "Completed adjacent M1 bars; regular session only; stale data never signals.",
            "A 50/200 pair on M1 bars needs 201 completed bars of history before it can signal.",
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
        fast_period, slow_period = validated["fast_period"], validated["slow_period"]
        assert isinstance(fast_period, int) and isinstance(slow_period, int)
        require_param(fast_period < slow_period, "fast_period < slow_period", "fast_period")

        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, _required_bars(validated))
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
