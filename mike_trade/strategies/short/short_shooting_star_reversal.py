"""SHORT on bearish confirmation after a shooting star."""

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
)
from mike_trade.strategies.indicators.windows import is_shooting_star

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "short_shooting_star_reversal"


class ShortShootingStarReversal(ShortStrategy):
    """SHORT when the bar after an uptrend shooting star closes below its low."""

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version="1.0.0",
        side=StrategyRole.SHORT,
        category="technical",
        sub_category="price_action",
        reference_symbols=(),
        required_timeframes=(CandleTimeframe.M1,),
        warmup_bars=8,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=CandleTimeframe.M1,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec(
                    "trend_bars",
                    ParameterKind.INT,
                    5,
                    2,
                    50,
                    description="Bars over which the pre-star close must have risen. Default 5.",
                ),
                ParameterSpec(
                    "shadow_ratio",
                    ParameterKind.DECIMAL,
                    Decimal(2),
                    Decimal(1),
                    Decimal(10),
                    description="Minimum upper shadow as a multiple of real body. Default 2.",
                ),
                ParameterSpec(
                    "max_lower_shadow",
                    ParameterKind.DECIMAL,
                    Decimal("0.1"),
                    Decimal(0),
                    Decimal("0.5"),
                    description="Maximum lower shadow as a fraction of range. Default 0.1.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=(
            "SHORT on the bar after a shooting star (upper shadow at least twice its "
            "body and little lower shadow) following a rise, when close finishes below "
            "the shooting star's low."
        ),
        source=(
            "Steve Nison, Japanese Candlestick Charting Techniques (2nd ed., 2001); "
            "StockCharts ChartSchool, Candlestick Bearish Reversal Patterns, Shooting Star: "
            "https://chartschool.stockcharts.com/table-of-contents/chart-analysis/candlestick-charts/candlestick-bearish-reversal-patterns"
        ),
        execution_limitations=(
            "Daily candlestick method adapted to M1; an opening gap is not "
            "required, consistent with Nison's examples.",
            "Bearish confirmation is objectively defined as the next close "
            "below the star low; the three final bars must be adjacent.",
            "SHORT entry on completed regular-session bars only; engine owns execution.",
        ),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        trend = cls.spec.parameter_schema.validate(params)["trend_bars"]
        assert isinstance(trend, int)
        return trend + 3

    def evaluate(
        self, context: MarketContext, params: Mapping[str, object], state: Mapping[str, object]
    ) -> StrategyEvaluation:
        values = self.spec.parameter_schema.validate(params)
        trend = values["trend_bars"]
        ratio, lower = values["shadow_ratio"], values["max_lower_shadow"]
        assert isinstance(trend, int) and isinstance(ratio, Decimal) and isinstance(lower, Decimal)
        bars = closed_bars(self.spec, context, trend + 3)
        if bars is None:
            return insufficient(self.spec, context)
        reason = gate(self.spec, context, state)
        if reason is not None:
            return blocked(self.spec, context, reason)
        window = bars[-trend - 3 :]
        if any(not adjacent(a, b) for a, b in zip(window, window[1:], strict=False)):
            return insufficient(self.spec, context)
        before, star, latest = bars[-3], bars[-2], bars[-1]
        triggered = (
            is_shooting_star(star, ratio, lower)
            and before.close > bars[-3 - trend].close
            and latest.close < star.low
        )
        return finish(self.spec, context, latest, state, triggered)
