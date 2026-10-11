"""Exit when a newly confirmed swing low is below the preceding swing low."""

from __future__ import annotations

from collections.abc import Mapping
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


class LowerLowConfirmationSell(SellRule):
    spec = StrategySpec(
        strategy_id="lower_low_confirmation",
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
            "SELL when the five-bar fractal low formed two bars earlier is confirmed on "
            "the latest completed bar strictly below the preceding confirmed five-bar fractal low "
            "in the lookback."
        ),
        source=(
            "Charles H. Dow trend structure (successive lower lows); five-bar pivot "
            "confirmation follows Bill Williams, Trading Chaos, and TradingView "
            "Williams Fractal: https://www.tradingview.com/support/solutions/"
            "43000591663-williams-fractal-indicator/"
        ),
        execution_limitations=(
            "Signals only when the newer pivot becomes knowable, after two right-hand "
            "bars close; no future observation is read.",
            "The complete structure window must be consecutive completed regular-session M1 bars.",
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
        triggered = (
            len(pivots) >= 2
            and pivots[-1] == len(bars) - 3
            and bars[pivots[-1]].low < bars[pivots[-2]].low
        )
        return finish(self.spec, context, bars[-1], state, triggered)
