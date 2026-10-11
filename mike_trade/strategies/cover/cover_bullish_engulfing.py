"""COVER on a bullish real-body engulfing pattern."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import (
    CoverStrategy,
    ParameterSchema,
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
    finish,
    gate,
    insufficient,
)
from mike_trade.strategies.indicators.finite import engulfing

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "cover_bullish_engulfing"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


class CoverBullishEngulfing(CoverStrategy):
    """COVER when a bearish candle is followed by a bullish candle that engulfs its real body.

    Prior bar ``t-1`` and latest completed bar ``t``::

        close[t-1] < open[t-1]  AND  close[t] > open[t]
        AND  open[t] <= close[t-1]  AND  close[t] >= open[t-1]        ->  COVER

    Real bodies only (shadows are ignored), the shared ``engulfing`` helper.  The only context the
    rule needs is the two adjacent completed bars themselves: the pattern is complete, and so
    observable, at the close of bar ``t``.  No separate trend filter is applied.  Finite window,
    no persisted state.  Needs 2 completed bars.
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version=STRATEGY_VERSION,
        side=StrategyRole.COVER,
        category="technical",
        sub_category="price_action",
        reference_symbols=(),
        required_timeframes=(TIMEFRAME,),
        warmup_bars=2,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema(()),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=(
            "COVER when a bearish candle is followed by a bullish candle whose real body fully "
            "engulfs the prior real body. No shadow engulfment or separate trend filter is "
            "required. Pattern trigger only; no alpha or profitability claim."
        ),
        source=(
            "StockCharts ChartSchool, Candlestick Bullish Reversal Patterns (bullish engulfing): "
            "https://chartschool.stockcharts.com/table-of-contents/chart-analysis/"
            "candlestick-charts/candlestick-bullish-reversal-patterns. Same definition as the "
            "engulfing_candle_reversal BUY."
        ),
        execution_limitations=(
            "Exit signal only: the engine must evaluate it only while a managed short exists and decides the cover order, quantity and any follow-on action; it never implies a BUY.",
            "Both bars must be consecutive completed regular-session M1 bars; stale data never signals.",
            "The pattern alone is not confirmation of a broader reversal; no trend or volume filter.",
        ),
        reason_codes=REASON_CODES,
    )

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        self.spec.parameter_schema.validate(params)
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, 2)
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        prior, latest = bars[-2:]
        if not adjacent(prior, latest):
            return insufficient(self.spec, context)
        return finish(self.spec, context, latest, state, engulfing(prior, latest, bullish=True))
