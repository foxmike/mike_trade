"""SHORT on a bearish real-body engulfing pattern in consecutive M1 bars."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import (
    ParameterKind,
    ParameterSchema,
    ParameterSpec,
    ShortStrategy,
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

STRATEGY_ID = "short_bearish_engulfing"
STRATEGY_VERSION = "1.0.0"
#: Declared trigger and only required timeframe (see `_SPEC`).
TIMEFRAME = CandleTimeframe.M1
WARMUP_BARS = 5

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.SHORT,
    category="technical",
    sub_category="price_action",
    reference_symbols=(),
    required_timeframes=(TIMEFRAME,),
    warmup_bars=WARMUP_BARS,
    completed_bars_only=True,
    requires_volume=False,
    trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
    trigger_timeframe=TIMEFRAME,
    parameter_schema=ParameterSchema(
        (
            ParameterSpec(
                "trend_bars",
                ParameterKind.INT,
                3,
                2,
                50,
                description="Bars of prior close-to-close advance before engulfment. Default 3.",
            ),
        )
    ),
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=(
        "SHORT when a bullish candle following a prior advance is followed by a "
        "bearish candle whose real body fully engulfs it."
    ),
    source=(
        "StockCharts ChartSchool, Candlestick Bearish Reversal Patterns: "
        "https://chartschool.stockcharts.com/table-of-contents/chart-analysis/candlestick-charts/candlestick-bearish-reversal-patterns"
    ),
    execution_limitations=(
        "SHORT entry signal only; the engine owns execution and risk.",
        "The entire advance and two pattern bars must be adjacent completed "
        "M1 bars; the evaluation context must be regular-session.",
    ),
    reason_codes=REASON_CODES,
)


class ShortBearishEngulfing(ShortStrategy):
    spec = _SPEC

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        trend = cls.spec.parameter_schema.validate(params)["trend_bars"]
        assert isinstance(trend, int)
        return trend + 2

    def evaluate(
        self, context: MarketContext, params: Mapping[str, object], state: Mapping[str, object]
    ) -> StrategyEvaluation:
        trend = self.spec.parameter_schema.validate(params)["trend_bars"]
        assert isinstance(trend, int)
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, trend + 2)
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        window = bars[-trend - 2 :]
        prior, latest = window[-2:]
        if any(not adjacent(a, b) for a, b in zip(window, window[1:], strict=False)):
            return insufficient(self.spec, context)
        triggered = prior.close > window[0].close and engulfing(prior, latest, bullish=False)
        return finish(self.spec, context, latest, state, triggered)
