"""COVER when the fast EMA crosses above the slow EMA."""

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
    STATE_INDICATOR,
    STATE_LAST_SIGNAL_BAR,
    adjacent,
    advance,
    blocked,
    closed_bars,
    crossed_above,
    finish,
    gate,
    insufficient,
    known,
    parts,
    require_param,
)
from mike_trade.strategies.indicators.core import MACDCalculator

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "cover_ema_bullish_crossover"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


def _required_bars(validated: Mapping[str, object]) -> int:
    slow_period = validated["slow_period"]
    assert isinstance(slow_period, int)
    return slow_period + 1


class CoverEmaBullishCrossover(CoverStrategy):
    """COVER when EMA(`fast_period`) crosses above EMA(`slow_period`) on a completed bar.

    ``EMA[t]`` uses alpha = 2 / (n + 1), seeded with the SMA of the first n closes.  Signal (edge,
    prior bar ``t-1`` and latest completed bar ``t``)::

        fast[t-1] <= slow[t-1]  AND  fast[t] > slow[t]  ->  COVER

    Both EMAs are recursive: they are resumed from the strategy's own persisted
    ``indicator_state`` bar by bar (never recomputed from the bounded candle window); absent,
    foreign-parameter or corrupt state is re-seeded from the supplied history.  Needs
    ``slow_period + 1`` completed bars (27 by default).
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version=STRATEGY_VERSION,
        side=StrategyRole.COVER,
        category="technical",
        sub_category="moving_average",
        reference_symbols=(),
        required_timeframes=(TIMEFRAME,),
        warmup_bars=27,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec(
                    "fast_period", ParameterKind.INT, 12, 2, 100,
                    description="Fast EMA length of closes. Default 12.",
                ),
                ParameterSpec(
                    "slow_period", ParameterKind.INT, 26, 3, 200,
                    description="Slow EMA length of closes; must exceed fast_period. Default 26.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_INDICATOR),
        methodology=(
            "COVER when the fast EMA crosses above the slow EMA on a completed bar "
            "(EMA crossover, bullish). Restates a published textbook "
            "definition; no alpha or profitability claim."
        ),
        source=(
            "Moving-average crossover as documented in StockCharts ChartSchool, 'Moving Averages - "
            "Simple and Exponential' (EMA alpha = 2 / (n + 1); crossovers of a shorter over a "
            "longer average); 12/26 periods follow Gerald Appel's MACD line components."
        ),
        execution_limitations=(
            "Exit signal only: the engine must evaluate it only while a managed short exists and decides the cover order, quantity and any follow-on action; it never implies a BUY.",
            "Completed adjacent M1 bars; regular session only; stale data never signals.",
            "No stop, target or position handling is performed by the strategy.",
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
        adv = advance(
            state, bars, f"{STRATEGY_ID}|{STRATEGY_VERSION}|{fast_period}|{slow_period}",
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
        triggered = crossed_above(fast_prior, slow_prior, fast_latest, slow_latest)
        return finish(self.spec, context, bars[-1], state, triggered, adv.updates)
