"""Short entry when close breaks below the displaced Ichimoku cloud."""

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
    require_param,
)
from mike_trade.strategies.indicators.windows import ichimoku_cloud

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "short_ichimoku_bearish_breakdown"
TIMEFRAME = CandleTimeframe.M1
TENKAN, KIJUN, SENKOU_B, DISPLACEMENT = 9, 26, 52, 26
BARS_NEEDED = SENKOU_B + DISPLACEMENT + 1


def _required_bars(validated: Mapping[str, object]) -> int:
    tenkan = validated["tenkan_period"]
    kijun = validated["kijun_period"]
    senkou_b = validated["senkou_b_period"]
    displacement = validated["displacement"]
    assert isinstance(tenkan, int)
    assert isinstance(kijun, int)
    assert isinstance(senkou_b, int)
    assert isinstance(displacement, int)
    return max(tenkan, kijun, senkou_b) + displacement + 1


class ShortIchimokuBearishBreakdown(ShortStrategy):
    """SHORT when close crosses below the lower edge of the point-in-time cloud."""

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version="1.0.0",
        side=StrategyRole.SHORT,
        category="technical",
        sub_category="trend_following",
        reference_symbols=(),
        required_timeframes=(TIMEFRAME,),
        warmup_bars=BARS_NEEDED,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec(
                    "tenkan_period",
                    ParameterKind.INT,
                    TENKAN,
                    2,
                    100,
                    description="Tenkan-sen range length. Default 9.",
                ),
                ParameterSpec(
                    "kijun_period",
                    ParameterKind.INT,
                    KIJUN,
                    3,
                    200,
                    description="Kijun-sen range length. Default 26.",
                ),
                ParameterSpec(
                    "senkou_b_period",
                    ParameterKind.INT,
                    SENKOU_B,
                    4,
                    400,
                    description="Senkou Span B range length. Default 52.",
                ),
                ParameterSpec(
                    "displacement",
                    ParameterKind.INT,
                    DISPLACEMENT,
                    1,
                    200,
                    description="Forward displacement for causal cloud alignment. Default 26.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=(
            "SHORT when close crosses from at-or-above to below the lower edge of the "
            "displaced Ichimoku cloud (minimum of Senkou Span A and B)."
        ),
        source=(
            "Goichi Hosoda's Ichimoku Kinko Hyo; StockCharts ChartSchool, Ichimoku Cloud "
            "(Tenkan 9, Kijun 26, Senkou B 52, spans plotted 26 periods ahead): "
            "https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-overlays/ichimoku-cloud"
        ),
        execution_limitations=(
            "Defaults are the canonical 9/26/52/26 settings adapted to M1; Chikou is "
            "excluded because using its future plot would look ahead.",
            "The cloud at each signal bar uses only observations ending 26 "
            "bars earlier; the crossing bars must be adjacent.",
            "Advisory short entry on completed regular-session bars only.",
        ),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        return _required_bars(cls.spec.parameter_schema.validate(params))

    def evaluate(
        self, context: MarketContext, params: Mapping[str, object], state: Mapping[str, object]
    ) -> StrategyEvaluation:
        validated = self.spec.parameter_schema.validate(params)
        tenkan = validated["tenkan_period"]
        kijun = validated["kijun_period"]
        senkou_b = validated["senkou_b_period"]
        displacement = validated["displacement"]
        assert isinstance(tenkan, int)
        assert isinstance(kijun, int)
        assert isinstance(senkou_b, int)
        assert isinstance(displacement, int)
        require_param(tenkan <= kijun, "tenkan_period <= kijun_period", "tenkan_period")
        require_param(kijun <= senkou_b, "kijun_period <= senkou_b_period", "kijun_period")
        need = _required_bars(validated)
        bars = closed_bars(self.spec, context, need)
        if bars is None:
            return insufficient(self.spec, context)
        reason = gate(self.spec, context, state)
        if reason is not None:
            return blocked(self.spec, context, reason)
        window = bars[-need:]
        if any(not adjacent(a, b) for a, b in zip(window, window[1:], strict=False)):
            return insufficient(self.spec, context)
        prior_bottom = min(ichimoku_cloud(window[:-1], tenkan, kijun, senkou_b, displacement))
        latest_bottom = min(ichimoku_cloud(window, tenkan, kijun, senkou_b, displacement))
        return finish(
            self.spec,
            context,
            bars[-1],
            state,
            bars[-2].close >= prior_bottom and bars[-1].close < latest_bottom,
        )
