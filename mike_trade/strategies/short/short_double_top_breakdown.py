"""SHORT when a confirmed double top breaks its intervening support."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
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
    require_param,
)
from mike_trade.strategies.indicators.windows import swing_pivots

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "short_double_top_breakdown"


class ShortDoubleTopBreakdown(ShortStrategy):
    """SHORT when two similar confirmed peaks are completed by a neckline break."""

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version="1.0.0",
        side=StrategyRole.SHORT,
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
                    description="Bars on each side of each confirmed peak. Default 2.",
                ),
                ParameterSpec(
                    "lookback",
                    ParameterKind.INT,
                    60,
                    15,
                    300,
                    description="Prior bars searched for the two peaks. Default 60.",
                ),
                ParameterSpec(
                    "peak_tolerance",
                    ParameterKind.DECIMAL,
                    Decimal("0.03"),
                    Decimal(0),
                    Decimal("0.10"),
                    description="Maximum relative difference between peaks. Default 3%.",
                ),
                ParameterSpec(
                    "min_decline",
                    ParameterKind.DECIMAL,
                    Decimal("0.10"),
                    Decimal("0.01"),
                    Decimal("0.50"),
                    description="Minimum peak-to-trough decline. Default 10%.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=(
            "SHORT when the latest two confirmed swing highs are within 3%, their "
            "confirmed intervening trough is at least 10% below the lower peak, and close crosses "
            "below that trough support."
        ),
        source=(
            "StockCharts ChartSchool, Double Top Reversal: peaks usually within 3%, trough "
            "decline generally 10-20%, and the pattern is complete only on a support break: "
            "https://chartschool.stockcharts.com/table-of-contents/chart-analysis/chart-patterns/double-top-reversal"
        ),
        execution_limitations=(
            "Daily/intermediate-term chart pattern adapted mechanically to M1; "
            "canonical percentage requirements are retained.",
            "Peaks use confirmed fractals and only prior completed bars; the "
            "latest bar must cross support and the whole window must be adjacent.",
            "Confirmed trough support is required; no volume confirmation or target is imposed. "
            "SHORT entry only.",
        ),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        lookback = cls.spec.parameter_schema.validate(params)["lookback"]
        assert isinstance(lookback, int)
        return lookback + 1

    def evaluate(
        self, context: MarketContext, params: Mapping[str, object], state: Mapping[str, object]
    ) -> StrategyEvaluation:
        values = self.spec.parameter_schema.validate(params)
        strength, lookback = values["strength"], values["lookback"]
        tolerance, decline = values["peak_tolerance"], values["min_decline"]
        assert isinstance(strength, int) and isinstance(lookback, int)
        assert isinstance(tolerance, Decimal) and isinstance(decline, Decimal)
        require_param(lookback >= 4 * strength + 3, "lookback >= 4 * strength + 3", "lookback")
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
        highs, lows = swing_pivots(structure, strength)
        triggered = False
        if len(highs) >= 2:
            first_i, first_peak = highs[-2]
            second_i, second_peak = highs[-1]
            troughs = [(i, value) for i, value in lows if first_i < i < second_i]
            if second_i > first_i + 1 and troughs:
                support = min(value for _, value in troughs)
                lower_peak = min(first_peak, second_peak)
                similar = abs(second_peak - first_peak) / first_peak <= tolerance
                deep_enough = (lower_peak - support) / lower_peak >= decline
                first_led_trend = first_peak == max(bar.high for bar in structure[: first_i + 1])
                not_invalidated = max(
                    (bar.high for bar in structure[second_i + 1 :]), default=second_peak
                ) <= max(first_peak, second_peak)
                triggered = (
                    similar
                    and deep_enough
                    and first_led_trend
                    and not_invalidated
                    and bars[-2].close >= support
                    and bars[-1].close < support
                )
        return finish(self.spec, context, bars[-1], state, triggered)
