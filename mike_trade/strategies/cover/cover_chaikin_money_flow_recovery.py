"""COVER when Chaikin Money Flow recovers from non-positive to positive."""

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
from mike_trade.strategies.indicators.finite import chaikin_money_flow

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "cover_chaikin_money_flow_recovery"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1
ZERO = Decimal(0)


class CoverChaikinMoneyFlowRecovery(CoverStrategy):
    """COVER when the rolling n-bar Chaikin Money Flow moves from at-or-below zero to above zero.

    Per bar the money-flow multiplier is ``((close - low) - (high - close)) / (high - low)``
    (zero for a zero-range bar); ``CMF = sum(multiplier * volume, n) / sum(volume, n)`` (shared
    ``chaikin_money_flow`` helper).  Recovery trigger (edge; each CMF is the n-bar window ending
    on prior bar ``t-1`` and latest completed bar ``t``)::

        CMF[t-1] <= 0  AND  CMF[t] > 0  ->  COVER

    That is, accumulation pressure takes over after a window of neutral-or-selling pressure; an
    exact zero on the prior bar counts as the non-positive side, as in the chaikin_money_flow
    BUY.  CMF is undefined when a window has no volume, which suppresses the signal.  Finite
    window, so no indicator state is persisted.  Needs ``period + 1`` completed bars (21 by
    default).
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version=STRATEGY_VERSION,
        side=StrategyRole.COVER,
        category="technical",
        sub_category="volume",
        reference_symbols=(),
        required_timeframes=(TIMEFRAME,),
        warmup_bars=21,
        completed_bars_only=True,
        requires_volume=True,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec(
                    "period", ParameterKind.INT, 20, 2, 50,
                    description="CMF look-back in completed bars. Default 20.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=(
            "COVER when n-period Chaikin Money Flow crosses from at-or-below zero to above "
            "zero (money-flow recovery). Rule definition only; no alpha or profitability "
            "claim."
        ),
        source=(
            "Marc Chaikin's CMF; StockCharts ChartSchool, Chaikin Money Flow: "
            "https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-"
            "overlays/technical-indicators/chaikin-money-flow-cmf. Same calculation and zero "
            "crossing as the chaikin_money_flow BUY."
        ),
        execution_limitations=(
            "Exit signal only: the engine must evaluate it only while a managed short exists and decides the cover order, quantity and any follow-on action; it never implies a BUY.",
            "Rolling money-flow volume over rolling volume; a zero high-low range contributes "
            "zero multiplier and a zero-volume window is undefined.",
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
        period = self.spec.parameter_schema.validate(params)["period"]
        assert isinstance(period, int)

        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, period + 1)
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context)
        prior = chaikin_money_flow(bars[-(period + 1):-1])
        latest = chaikin_money_flow(bars[-period:])
        if not known(prior, latest):
            return insufficient(self.spec, context)
        triggered = crossed_above(prior, ZERO, latest, ZERO)
        return finish(self.spec, context, bars[-1], state, triggered)
