"""Long exit when a confirmed head-and-shoulders top breaks its neckline."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
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
    require_param,
)
from mike_trade.strategies.indicators.windows import swing_pivots
from mike_trade.strategies.sell.base import SellRule

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "head_and_shoulders_breakdown"


class HeadAndShouldersBreakdownSell(SellRule):
    """SELL when a three-peak top closes through the line joining its reaction lows."""

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version="1.0.0",
        side=StrategyRole.SELL,
        category="technical",
        sub_category="price_action",
        reference_symbols=(),
        required_timeframes=(CandleTimeframe.M1,),
        warmup_bars=61,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=CandleTimeframe.M1,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec(
                    "strength",
                    ParameterKind.INT,
                    2,
                    1,
                    10,
                    description="Bars on each side of every confirmed peak. Default 2.",
                ),
                ParameterSpec(
                    "lookback",
                    ParameterKind.INT,
                    60,
                    20,
                    300,
                    description="Prior bars searched for the pattern. Default 60.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=(
            "SELL when three latest confirmed peaks form left shoulder, higher head, "
            "and lower right shoulder after a rising predecessor peak, and close crosses "
            "below the neckline through the two intervening reaction lows."
        ),
        source=(
            "StockCharts ChartSchool, Head and Shoulders Top: three successive peaks with the "
            "middle highest, reaction lows joined as a neckline, completed by neckline break: "
            "https://chartschool.stockcharts.com/table-of-contents/chart-analysis/chart-patterns/head-and-shoulders-top"
        ),
        execution_limitations=(
            "Shoulder symmetry and volume are preferred by the source but are "
            "not absolute requirements and are not added as arbitrary filters.",
            "All peaks are confirmed fractals; the neckline uses completed "
            "intervening lows and is extrapolated without look-ahead.",
            "The full lookback and crossing bars must be adjacent; advisory long exit only.",
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
        values = self.spec.parameter_schema.validate(params)
        strength, lookback = values["strength"], values["lookback"]
        assert isinstance(strength, int) and isinstance(lookback, int)
        require_param(lookback >= 6 * strength + 1, "lookback >= 6 * strength + 1", "lookback")
        bars = closed_bars(self.spec, context, lookback + 1)
        if bars is None:
            return insufficient(self.spec, context)
        bars = bars[-lookback - 1 :]
        reason = gate(self.spec, context, state)
        if reason is not None:
            return blocked(self.spec, context, reason)
        if any(not adjacent(a, b) for a, b in zip(bars, bars[1:], strict=False)):
            return insufficient(self.spec, context)
        structure = bars[:-1]
        highs, _ = swing_pivots(structure, strength)
        triggered = False
        if len(highs) >= 4:
            predecessor, left, head, right = highs[-4:]
            p_i, p_high = predecessor
            l_i, l_high = left
            h_i, h_high = head
            r_i, r_high = right
            shape = p_high < l_high and h_high > l_high and h_high > r_high
            if shape and l_i + 1 < h_i and h_i + 1 < r_i:
                low1 = min(range(l_i + 1, h_i), key=lambda i: structure[i].low)
                low2 = min(range(h_i + 1, r_i), key=lambda i: structure[i].low)
                with localcontext() as arithmetic:
                    arithmetic.prec, arithmetic.rounding = 28, ROUND_HALF_EVEN
                    slope = (structure[low2].low - structure[low1].low) / Decimal(low2 - low1)
                    prior_line = structure[low1].low + slope * Decimal(len(bars) - 2 - low1)
                    latest_line = structure[low1].low + slope * Decimal(len(bars) - 1 - low1)
                not_invalidated = (
                    max((bar.high for bar in structure[r_i + 1 :]), default=r_high) < h_high
                )
                triggered = (
                    not_invalidated
                    and bars[-2].close >= prior_line
                    and bars[-1].close < latest_line
                )
        return finish(self.spec, context, bars[-1], state, triggered)
