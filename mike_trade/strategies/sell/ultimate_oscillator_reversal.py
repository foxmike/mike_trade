"""Exit a long when Larry Williams' Ultimate Oscillator leaves overbought."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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
    crossed_below,
    finish,
    gate,
    insufficient,
    known,
    require_param,
)
from mike_trade.strategies.sell.base import SellRule

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext


def ultimate_oscillator(
    bars: Sequence[Candle], short: int, medium: int, long: int
) -> Decimal | None:
    if len(bars) != long + 1:
        raise ValueError("Ultimate Oscillator needs long period plus one bar")
    buying_pressure: list[Decimal] = []
    true_range: list[Decimal] = []
    for previous, bar in zip(bars, bars[1:], strict=False):
        floor = min(bar.low, previous.close)
        buying_pressure.append(bar.close - floor)
        true_range.append(max(bar.high, previous.close) - floor)
    with localcontext() as arithmetic:
        arithmetic.prec = 28
        arithmetic.rounding = ROUND_HALF_EVEN
        averages: list[Decimal] = []
        for period in (short, medium, long):
            denominator = sum(true_range[-period:], Decimal(0))
            if denominator == 0:
                return None
            averages.append(sum(buying_pressure[-period:], Decimal(0)) / denominator)
        return Decimal(100) * (4 * averages[0] + 2 * averages[1] + averages[2]) / Decimal(7)


class UltimateOscillatorReversalSell(SellRule):
    spec = StrategySpec(
        strategy_id="ultimate_oscillator_reversal",
        version="1.0.0",
        side=StrategyRole.SELL,
        category="technical",
        sub_category="oscillator",
        reference_symbols=(),
        required_timeframes=(CandleTimeframe.M1,),
        warmup_bars=30,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=CandleTimeframe.M1,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec("short_period", ParameterKind.INT, 7, 2, 50),
                ParameterSpec("medium_period", ParameterKind.INT, 14, 3, 100),
                ParameterSpec("long_period", ParameterKind.INT, 28, 4, 200),
                ParameterSpec(
                    "overbought", ParameterKind.DECIMAL, Decimal("70"), Decimal("1"), Decimal("99")
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=(
            "SELL when Ultimate Oscillator (7,14,28) crosses from at-or-above to below "
            "its conventional 70 overbought level."
        ),
        source=(
            "Larry Williams, Stocks & Commodities V.3:4, The Ultimate Oscillator (1985); "
            "StockCharts ChartSchool: https://chartschool.stockcharts.com/table-of-"
            "contents/technical-indicators-and-overlays/technical-indicators/"
            "ultimate-oscillator"
        ),
        execution_limitations=(
            "Uses Williams' 4:2:1 weighted buying-pressure/true-range formula; no "
            "divergence filter is added.",
            "Finite window; adjacent completed regular-session M1 crossing bars only.",
        ),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        values = cls.spec.parameter_schema.validate(params)
        short, medium, long = (
            values[name] for name in ("short_period", "medium_period", "long_period")
        )
        assert isinstance(short, int) and isinstance(medium, int) and isinstance(long, int)
        require_param(
            short < medium < long, "periods must increase: short < medium < long", "medium_period"
        )
        return long + 2

    def evaluate_trigger(
        self, context: MarketContext, params: Mapping[str, object], state: Mapping[str, object]
    ) -> StrategyEvaluation:
        values = self.spec.parameter_schema.validate(params)
        short, medium, long, threshold = (
            values["short_period"],
            values["medium_period"],
            values["long_period"],
            values["overbought"],
        )
        assert (
            isinstance(short, int)
            and isinstance(medium, int)
            and isinstance(long, int)
            and isinstance(threshold, Decimal)
        )
        require_param(
            short < medium < long, "periods must increase: short < medium < long", "medium_period"
        )
        bars = closed_bars(self.spec, context, long + 2)
        if bars is None:
            return insufficient(self.spec, context)
        reason = gate(self.spec, context, state)
        if reason is not None:
            return blocked(self.spec, context, reason)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context)
        prior = ultimate_oscillator(bars[-long - 2 : -1], short, medium, long)
        latest = ultimate_oscillator(bars[-long - 1 :], short, medium, long)
        if not known(prior, latest):
            return insufficient(self.spec, context)
        return finish(
            self.spec, context, bars[-1], state, crossed_below(prior, threshold, latest, threshold)
        )
