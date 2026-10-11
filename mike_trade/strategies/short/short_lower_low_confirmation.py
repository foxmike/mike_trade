"""SHORT when a newly confirmed swing low is below the preceding swing low."""

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
from mike_trade.strategies.indicators.windows import swing_pivots

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext


class ShortLowerLowConfirmation(ShortStrategy):
    spec = StrategySpec(
        strategy_id="short_lower_low_confirmation",
        version="1.0.0",
        side=StrategyRole.SHORT,
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
            (
                ParameterSpec(
                    "lookback",
                    ParameterKind.INT,
                    30,
                    7,
                    200,
                    description="Bars searched for two confirmed five-bar pivot lows. Default 30.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=(
            "SHORT when the five-bar fractal low formed two bars earlier is confirmed on "
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
            "The complete structure window must be consecutive completed M1 bars; "
            "the evaluation context must be regular-session.",
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
        _, lows = swing_pivots(bars, 2)
        pivots = [index for index, _ in lows]
        triggered = (
            len(pivots) >= 2
            and pivots[-1] == len(bars) - 3
            and bars[pivots[-1]].low < bars[pivots[-2]].low
        )
        return finish(self.spec, context, bars[-1], state, triggered)
