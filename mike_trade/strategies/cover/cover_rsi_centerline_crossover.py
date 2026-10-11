"""COVER when RSI crosses above the 50 centerline."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
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
)
from mike_trade.strategies.indicators.core import RSICalculator

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "cover_rsi_centerline_crossover"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1
CENTERLINE = Decimal(50)


def _required_bars(validated: Mapping[str, object]) -> int:
    rsi_period = validated["rsi_period"]
    assert isinstance(rsi_period, int)
    return rsi_period + 2


class CoverRsiCenterlineCrossover(CoverStrategy):
    """COVER when the Wilder RSI crosses above the 50 centerline on a completed bar.

    Signal (edge, prior bar ``t-1`` and latest completed bar ``t``)::

        rsi[t-1] <= 50  AND  rsi[t] > 50  ->  COVER

    The RSI is recursive and resumed from the strategy's persisted ``indicator_state`` bar by
    bar; absent, foreign-parameter or corrupt state is re-seeded from the supplied history.
    Needs ``rsi_period + 2`` completed bars (16 by default).
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version=STRATEGY_VERSION,
        side=StrategyRole.COVER,
        category="technical",
        sub_category="oscillator",
        reference_symbols=(),
        required_timeframes=(TIMEFRAME,),
        warmup_bars=16,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec(
                    "rsi_period", ParameterKind.INT, 14, 2, 100,
                    description="Wilder RSI length in completed bars. Default 14.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_INDICATOR),
        methodology=(
            "COVER when the Wilder RSI crosses above the 50 centerline on a completed bar "
            "(prior RSI <= 50 and latest RSI > 50). Restates a published textbook "
            "definition; no alpha or profitability claim."
        ),
        source=(
            "J. Welles Wilder, 'New Concepts in Technical Trading Systems' (1978) as "
            "documented in StockCharts ChartSchool, 'Relative Strength Index (RSI)' (14-period "
            "Wilder smoothing; 50 centerline). Same calculation as the "
            "rsi_centerline_crossover BUY."
        ),
        execution_limitations=(
            "Exit signal only: the engine must evaluate it only while a managed short exists and decides the cover order, quantity and any follow-on action; it never implies a BUY.",
            "Completed adjacent M1 bars; regular session only; stale data never signals.",
            "The 50 centerline is fixed; a slow upward drift can keep a short open until RSI "
            "actually closes above it.",
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
        rsi_period = validated["rsi_period"]
        assert isinstance(rsi_period, int)

        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, _required_bars(validated))
        if bars is None:
            return insufficient(self.spec, context)
        adv = advance(
            state, bars, f"{STRATEGY_ID}|{STRATEGY_VERSION}|{rsi_period}",
            fresh=lambda: RSICalculator(rsi_period),
            restore=RSICalculator.restore,
            feed=lambda calc, bar: calc.update(bar.close),
        )
        if reason is not None:
            return blocked(self.spec, context, reason, adv.updates)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context, adv.updates)
        if not known(adv.prior, adv.latest):
            return insufficient(self.spec, context, adv.updates)
        triggered = crossed_above(adv.prior, CENTERLINE, adv.latest, CENTERLINE)
        return finish(self.spec, context, bars[-1], state, triggered, adv.updates)
