"""Long exit when Chaikin's Accumulation/Distribution Line breaks support."""

from __future__ import annotations

from collections.abc import Mapping
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
    STATE_INDICATOR,
    STATE_LAST_SIGNAL_BAR,
    adjacent,
    advance,
    blocked,
    closed_bars,
    crossed_below,
    finish,
    gate,
    insufficient,
    parts,
)
from mike_trade.strategies.indicators.volume_flow import AccumulationDistributionCalculator
from mike_trade.strategies.sell.base import SellRule

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "accumulation_distribution_breakdown"


class AccumulationDistributionBreakdownSell(SellRule):
    """SELL when the cumulative A/D Line crosses below rolling support."""

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version="1.0.0",
        side=StrategyRole.SELL,
        category="technical",
        sub_category="volume",
        reference_symbols=(),
        required_timeframes=(CandleTimeframe.M1,),
        warmup_bars=22,
        completed_bars_only=True,
        requires_volume=True,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=CandleTimeframe.M1,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec(
                    "support_period",
                    ParameterKind.INT,
                    20,
                    2,
                    200,
                    description="Prior A/D Line bars defining support. Default 20.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_INDICATOR),
        methodology=(
            "SELL when the cumulative Accumulation/Distribution Line crosses below "
            "its lowest reading in the preceding support_period bars."
        ),
        source=(
            "Marc Chaikin's Cumulative Money Flow/Accumulation Distribution Line; "
            "StockCharts ChartSchool, Accumulation/Distribution Line (MFV formula, running "
            "total, downtrend as distribution): "
            "https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/accumulation-distribution-line"
        ),
        execution_limitations=(
            "A/D is snapshot-restored and never restarted at a bounded history "
            "edge; its arbitrary additive origin does not affect support breaks.",
            "A zero-range bar contributes zero money-flow volume; crossing bars "
            "must be adjacent completed regular-session M1 bars.",
        ),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        period = cls.spec.parameter_schema.validate(params)["support_period"]
        assert isinstance(period, int)
        return period + 2

    def evaluate_trigger(
        self, context: MarketContext, params: Mapping[str, object], state: Mapping[str, object]
    ) -> StrategyEvaluation:
        period = self.spec.parameter_schema.validate(params)["support_period"]
        assert isinstance(period, int)
        bars = closed_bars(self.spec, context, period + 2)
        if bars is None:
            return insufficient(self.spec, context)
        if any(not adjacent(a, b) for a, b in zip(bars, bars[1:], strict=False)):
            return insufficient(self.spec, context)
        reason = gate(self.spec, context, state)
        adv = advance(
            state,
            bars,
            f"{STRATEGY_ID}|1.0.0|{period}",
            fresh=lambda: AccumulationDistributionCalculator(period),
            restore=AccumulationDistributionCalculator.restore,
            feed=lambda calc, bar: calc.update(bar),
        )
        if reason is not None:
            return blocked(self.spec, context, reason, adv.updates)
        prior_line, prior_support = parts(adv.prior, 2)
        line, support = parts(adv.latest, 2)
        if prior_support is None or support is None:
            return insufficient(self.spec, context, adv.updates)
        return finish(
            self.spec,
            context,
            bars[-1],
            state,
            crossed_below(prior_line, prior_support, line, support),
            adv.updates,
        )
