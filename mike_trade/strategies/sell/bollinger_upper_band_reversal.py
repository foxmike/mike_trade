"""Exit when a close outside the upper Bollinger Band reverses inside."""

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
from mike_trade.strategies.indicators.finite import bollinger
from mike_trade.strategies.sell.base import SellRule

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext


class BollingerUpperBandReversalSell(SellRule):
    spec = StrategySpec(
        strategy_id="bollinger_upper_band_reversal",
        version="1.0.0",
        side=StrategyRole.SELL,
        category="technical",
        sub_category="volatility",
        reference_symbols=(),
        required_timeframes=(CandleTimeframe.M1,),
        warmup_bars=21,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=CandleTimeframe.M1,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec("period", ParameterKind.INT, 20, 2, 200),
                ParameterSpec(
                    "deviations", ParameterKind.DECIMAL, Decimal("2"), Decimal("0.1"), Decimal("5")
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=(
            "SELL when close returns strictly inside the upper Bollinger Band after the "
            "prior close was strictly above its upper band."
        ),
        source=(
            "John Bollinger, Bollinger on Bollinger Bands; official band formula: "
            "https://www.bollingerbands.com/bollinger-bands; reversal setup described "
            "in StockCharts ChartSchool, Bollinger Bands."
        ),
        execution_limitations=(
            "A band tag alone is not a signal: this rule requires a completed close "
            "outside followed by a completed close back inside.",
            "Population standard deviation; adjacent completed regular-session M1 bars only.",
        ),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        period = cls.spec.parameter_schema.validate(params)["period"]
        assert isinstance(period, int)
        return period + 1

    def evaluate_trigger(
        self, context: MarketContext, params: Mapping[str, object], state: Mapping[str, object]
    ) -> StrategyEvaluation:
        values = self.spec.parameter_schema.validate(params)
        period, deviations = values["period"], values["deviations"]
        assert isinstance(period, int) and isinstance(deviations, Decimal)
        bars = closed_bars(self.spec, context, period + 1)
        if bars is None:
            return insufficient(self.spec, context)
        reason = gate(self.spec, context, state)
        if reason is not None:
            return blocked(self.spec, context, reason)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context)
        prior_upper = bollinger([bar.close for bar in bars[-period - 1 : -1]], period, deviations)[
            2
        ]
        latest_upper = bollinger([bar.close for bar in bars[-period:]], period, deviations)[2]
        return finish(
            self.spec,
            context,
            bars[-1],
            state,
            bars[-2].close > prior_upper and bars[-1].close < latest_upper,
        )
