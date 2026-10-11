"""COVER when RSI rises back above its oversold level."""

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

STRATEGY_ID = "cover_rsi_oversold_reversal"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


def _required_bars(validated: Mapping[str, object]) -> int:
    rsi_period = validated["rsi_period"]
    assert isinstance(rsi_period, int)
    return rsi_period + 2


class CoverRsiOversoldReversal(CoverStrategy):
    """COVER when the Wilder RSI, at or below the oversold level on the prior bar, closes back
    above it on the latest completed bar.

    Wilder RSI (SMA seed, then ``avg = (prev * (n - 1) + current) / n``).  Signal (edge)::

        rsi[t-1] <= oversold  AND  rsi[t] > oversold  ->  COVER

    This is the oversold *recovery* rule of the rsi_oversold_reversal BUY, not the inverse of an
    overbought exit.  The RSI is recursive and resumed from the strategy's persisted
    ``indicator_state`` bar by bar; absent, foreign-parameter or corrupt state is re-seeded from
    the supplied history.  Needs ``rsi_period + 2`` completed bars (16 by default).
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
                ParameterSpec(
                    "oversold", ParameterKind.DECIMAL, Decimal(30), Decimal(1), Decimal(50),
                    description="Oversold level; RSI must rise back above it. Default 30.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_INDICATOR),
        methodology=(
            "COVER when the Wilder RSI, at or below the oversold level on the prior bar, "
            "closes back above it on the latest completed bar. Restates a published textbook "
            "definition; no alpha or profitability claim."
        ),
        source=(
            "J. Welles Wilder, 'New Concepts in Technical Trading Systems' (1978) as "
            "documented in StockCharts ChartSchool, 'Relative Strength Index (RSI)' (14-period "
            "Wilder smoothing; 30 oversold). Same calculation and trigger as the "
            "rsi_oversold_reversal BUY."
        ),
        execution_limitations=(
            "Exit signal only: the engine must evaluate it only while a managed short exists and decides the cover order, quantity and any follow-on action; it never implies a BUY.",
            "Completed adjacent M1 bars; regular session only; stale data never signals.",
            "Only the single-step re-cross of the oversold level is used; no divergence or "
            "failure-swing confirmation.",
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
        rsi_period, oversold = validated["rsi_period"], validated["oversold"]
        assert isinstance(rsi_period, int) and isinstance(oversold, Decimal)

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
        triggered = crossed_above(adv.prior, oversold, adv.latest, oversold)
        return finish(self.spec, context, bars[-1], state, triggered, adv.updates)
