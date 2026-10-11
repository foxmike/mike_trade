"""Exit when close crosses below an objectively defined rising support line."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import Candle, CandleTimeframe
from mike_trade.strategies.base import (
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
    finish,
    gate,
    insufficient,
)
from mike_trade.strategies.sell.base import SellRule

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext


def _pivot_lows(bars: tuple[Candle, ...]) -> list[int]:
    return [
        index
        for index in range(2, len(bars) - 2)
        if bars[index].low
        < min(bars[index - 2].low, bars[index - 1].low, bars[index + 1].low, bars[index + 2].low)
    ]


class TrendlineBreakdownSell(SellRule):
    spec = StrategySpec(
        strategy_id="trendline_breakdown",
        version="1.0.0",
        side=StrategyRole.SELL,
        category="technical",
        sub_category="price_action",
        reference_symbols=(),
        required_timeframes=(CandleTimeframe.M1,),
        warmup_bars=31,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=CandleTimeframe.M1,
        parameter_schema=ParameterSchema(
            (ParameterSpec("lookback", ParameterKind.INT, 30, 7, 200),)
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=(
            "SELL when close crosses below the rising straight support line through the "
            "two latest confirmed five-bar pivot lows."
        ),
        source=(
            "John J. Murphy, Technical Analysis of the Financial Markets, trendline "
            "construction and violation; pivots use Bill Williams' five-bar fractal "
            "definition: https://www.tradingview.com/support/solutions/"
            "43000591663-williams-fractal-indicator/"
        ),
        execution_limitations=(
            "Only a line through two successively higher confirmed lows is eligible; "
            "equality is not a break.",
            "All lookback bars must be consecutive; pivots use two already-completed "
            "right-hand bars.",
        ),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        lookback = cls.spec.parameter_schema.validate(params)["lookback"]
        assert isinstance(lookback, int)
        return lookback + 1

    def evaluate_trigger(
        self, context: MarketContext, params: Mapping[str, object], state: Mapping[str, object]
    ) -> StrategyEvaluation:
        lookback = self.spec.parameter_schema.validate(params)["lookback"]
        assert isinstance(lookback, int)
        bars = closed_bars(self.spec, context, lookback + 1)
        if bars is None:
            return insufficient(self.spec, context)
        bars = bars[-lookback - 1 :]
        reason = gate(self.spec, context, state)
        if reason is not None:
            return blocked(self.spec, context, reason)
        if any(not adjacent(first, second) for first, second in zip(bars, bars[1:], strict=False)):
            return insufficient(self.spec, context)
        pivots = _pivot_lows(bars)
        triggered = False
        if len(pivots) >= 2:
            first, second = pivots[-2:]
            if bars[second].low > bars[first].low:
                with localcontext() as arithmetic:
                    arithmetic.prec = 28
                    arithmetic.rounding = ROUND_HALF_EVEN
                    slope = (bars[second].low - bars[first].low) / Decimal(second - first)
                    prior_line = bars[first].low + slope * Decimal(len(bars) - 2 - first)
                    latest_line = bars[first].low + slope * Decimal(len(bars) - 1 - first)
                triggered = bars[-2].close >= prior_line and bars[-1].close < latest_line
        return finish(self.spec, context, bars[-1], state, triggered)
