"""COVER when CCI recovers above its oversold boundary."""

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
from mike_trade.strategies.indicators.core import cci

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "cover_cci_oversold_reversal"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


class CoverCciOversoldReversal(CoverStrategy):
    """COVER when CCI, at or below the oversold level on the prior bar, rises back above it.

    ``CCI = (TP - SMA(TP, n)) / (0.015 * mean deviation)`` with typical price ``TP = (high + low
    + close) / 3`` (shared ``cci`` helper; undefined at zero mean deviation).  Signal (edge)::

        cci[t-1] <= oversold  AND  cci[t] > oversold  ->  COVER

    The default level is the conventional -100 used by the cci_reversion BUY.  Finite window, so
    no indicator state is persisted.  Needs ``period + 1`` completed bars (21 by default).
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version=STRATEGY_VERSION,
        side=StrategyRole.COVER,
        category="technical",
        sub_category="mean_reversion",
        reference_symbols=(),
        required_timeframes=(TIMEFRAME,),
        warmup_bars=21,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec(
                    "period", ParameterKind.INT, 20, 2, 50,
                    description="CCI look-back in completed bars. Default 20.",
                ),
                ParameterSpec(
                    "oversold", ParameterKind.DECIMAL, Decimal(-100), Decimal(-300), Decimal(-50),
                    description="Oversold level; CCI must rise back above it. Default -100.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=(
            "COVER on a CCI cross back above the oversold level after an oversold reading: "
            "CCI[t-1] <= -100 and CCI[t] > -100 by default. Rule definition only; no alpha or "
            "profitability claim."
        ),
        source=(
            "Donald Lambert, Commodity Channel Index; StockCharts ChartSchool, Commodity "
            "Channel Index (CCI): https://chartschool.stockcharts.com/table-of-contents/"
            "technical-indicators-and-overlays/technical-indicators/commodity-channel-index-cci. "
            "Same calculation and trigger as the cci_reversion BUY."
        ),
        execution_limitations=(
            "Exit signal only: the engine must evaluate it only while a managed short exists and decides the cover order, quantity and any follow-on action; it never implies a BUY.",
            "CCI uses typical price, Lambert's 0.015 constant and finite completed-bar windows; "
            "zero mean deviation is undefined and never signals.",
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
        values = cci(bars[-(period + 1):], period)
        if not known(values[-2], values[-1]):
            return insufficient(self.spec, context)
        triggered = crossed_above(values[-2], oversold, values[-1], oversold)
        return finish(self.spec, context, bars[-1], state, triggered)
