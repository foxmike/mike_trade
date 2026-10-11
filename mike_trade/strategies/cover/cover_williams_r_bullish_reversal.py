"""COVER when Williams %R rises back above its oversold level."""

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
from mike_trade.strategies.indicators.core import williams_r

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "cover_williams_r_bullish_reversal"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


class CoverWilliamsRBullishReversal(CoverStrategy):
    """COVER when Williams %R, at or below the oversold level on the prior bar, rises above it.

    ``%R = -100 * (highest high(n) - close) / (highest high(n) - lowest low(n))`` (shared
    ``williams_r`` helper; -100 is the most oversold).  Signal (edge)::

        %R[t-1] <= oversold  AND  %R[t] > oversold  ->  COVER      (default oversold = -80)

    The single-step re-cross of -80 is the trigger selected by the williams_r_reversion BUY (the
    sources differ on the trigger; the -50 follow-through variant needs an unspecified
    look-back).  A flat range leaves %R undefined and suppresses the signal.  Finite window, so
    no indicator state is persisted.  Needs ``period + 1`` completed bars (15 by default).
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version=STRATEGY_VERSION,
        side=StrategyRole.COVER,
        category="technical",
        sub_category="oscillator",
        reference_symbols=(),
        required_timeframes=(TIMEFRAME,),
        warmup_bars=15,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec(
                    "period", ParameterKind.INT, 14, 2, 100,
                    description="Williams %R look-back in completed bars. Default 14.",
                ),
                ParameterSpec(
                    "oversold", ParameterKind.DECIMAL, Decimal(-80), Decimal(-100), Decimal(-50),
                    description="Oversold level; %R must rise back above it. Default -80.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=(
            "COVER when Williams %R rises back above the oversold level (-80) on a completed "
            "bar after being at or below it on the prior bar. Restates a published textbook "
            "definition; no alpha or profitability claim."
        ),
        source=(
            "Larry Williams' %R as documented in StockCharts ChartSchool, 'Williams %R' "
            "(formula, 14 default, -20/-80 levels); the -80 re-cross trigger follows the "
            "cTrader and TrendSpider indicator documentation (sources differ on the trigger). "
            "Same calculation and trigger as the williams_r_reversion BUY."
        ),
        execution_limitations=(
            "Exit signal only: the engine must evaluate it only while a managed short exists and decides the cover order, quantity and any follow-on action; it never implies a BUY.",
            "Only the single-step re-cross of the oversold level is used; no -50 follow-through "
            "or divergence confirmation.",
            "Completed adjacent M1 bars; regular session only; stale data never signals.",
        ),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        period = cls.spec.parameter_schema.validate(params)["period"]
        assert isinstance(period, int)
        return period + 1

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        validated = self.spec.parameter_schema.validate(params)
        period, oversold = validated["period"], validated["oversold"]
        assert isinstance(period, int) and isinstance(oversold, Decimal)

        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, period + 1)
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context)
        values = williams_r(bars[-(period + 1):], period)
        if not known(values[-2], values[-1]):
            return insufficient(self.spec, context)
        triggered = crossed_above(values[-2], oversold, values[-1], oversold)
        return finish(self.spec, context, bars[-1], state, triggered)
