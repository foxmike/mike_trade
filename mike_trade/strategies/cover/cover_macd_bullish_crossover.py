"""COVER when the MACD line crosses above its signal line."""

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

STRATEGY_ID = "cover_macd_bullish_crossover"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


def _required_bars(validated: Mapping[str, object]) -> int:
    slow_period, signal_period = validated["slow_period"], validated["signal_period"]
    assert isinstance(slow_period, int) and isinstance(signal_period, int)
    return slow_period + signal_period


class CoverMacdBullishCrossover(CoverStrategy):
    """COVER when the MACD line crosses above its signal line on a completed bar.

    ``MACD = EMA(fast) - EMA(slow)`` of closes and ``signal = EMA(signal_period)`` of MACD
    (shared ``MACDCalculator``).  Signal (edge, prior bar ``t-1`` and latest completed bar ``t``)::

        macd[t-1] <= signal[t-1]  AND  macd[t] > signal[t]  ->  COVER

    All three EMAs are recursive and resumed from the strategy's persisted ``indicator_state``
    bar by bar (never recomputed from the bounded candle window); absent, foreign-parameter or
    corrupt state is re-seeded from the supplied history.  Needs ``slow_period + signal_period``
    completed bars (35 by default).
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version=STRATEGY_VERSION,
        side=StrategyRole.COVER,
        category="technical",
        sub_category="momentum",
        reference_symbols=(),
        required_timeframes=(TIMEFRAME,),
        warmup_bars=35,
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
                ParameterSpec(
                    "signal_period", ParameterKind.INT, 9, 2, 100,
                    description="EMA length of the MACD line (signal line). Default 9.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_INDICATOR),
        methodology=(
            "COVER when the MACD line crosses above its signal line on a completed bar "
            "(bullish signal-line crossover). Restates a published textbook definition; no "
            "alpha or profitability claim."
        ),
        source=(
            "Gerald Appel's MACD as documented in StockCharts ChartSchool, 'MACD (Moving "
            "Average Convergence/Divergence Oscillator)' (12/26/9; signal-line crossover). "
            "Same calculation as the macd_signal_crossover BUY."
        ),
        execution_limitations=(
            "Exit signal only: the engine must evaluate it only while a managed short exists and decides the cover order, quantity and any follow-on action; it never implies a BUY.",
            "Completed adjacent M1 bars; regular session only; stale data never signals.",
            "The MACD line is available only after slow_period closes and the signal line after "
            "slow_period + signal_period - 1; earlier bars never signal.",
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
        signal_period = validated["signal_period"]
        assert isinstance(fast_period, int) and isinstance(slow_period, int)
        assert isinstance(signal_period, int)
        require_param(fast_period < slow_period, "fast_period < slow_period", "fast_period")

        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, _required_bars(validated))
        if bars is None:
            return insufficient(self.spec, context)
        adv = advance(
            state, bars,
            f"{STRATEGY_ID}|{STRATEGY_VERSION}|{fast_period}|{slow_period}|{signal_period}",
            fresh=lambda: MACDCalculator(fast_period, slow_period, signal_period),
            restore=MACDCalculator.restore,
            feed=lambda calc, bar: calc.update(bar.close),
        )
        if reason is not None:
            return blocked(self.spec, context, reason, adv.updates)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context, adv.updates)
        _, _, macd_prior, signal_prior = parts(adv.prior, 4)
        _, _, macd_latest, signal_latest = parts(adv.latest, 4)
        if not known(macd_prior, signal_prior, macd_latest, signal_latest):
            return insufficient(self.spec, context, adv.updates)
        triggered = crossed_above(macd_prior, signal_prior, macd_latest, signal_latest)
        return finish(self.spec, context, bars[-1], state, triggered, adv.updates)
