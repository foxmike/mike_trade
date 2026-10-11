"""Long exit on confirmed bearish price/momentum divergence."""

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

STRATEGY_ID = "bearish_momentum_divergence"


class BearishMomentumDivergenceSell(SellRule):
    """SELL when a higher confirmed price high has lower same-bar n-period momentum."""

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version="1.0.0",
        side=StrategyRole.SELL,
        category="technical",
        sub_category="oscillator",
        reference_symbols=(),
        required_timeframes=(CandleTimeframe.M1,),
        warmup_bars=60,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=CandleTimeframe.M1,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec(
                    "momentum_period",
                    ParameterKind.INT,
                    10,
                    2,
                    100,
                    description="Close-difference momentum lookback. Default 10.",
                ),
                ParameterSpec(
                    "strength",
                    ParameterKind.INT,
                    2,
                    1,
                    10,
                    description="Completed bars on each side of a confirmed pivot. Default 2.",
                ),
                ParameterSpec(
                    "lookback",
                    ParameterKind.INT,
                    50,
                    10,
                    300,
                    description="Bars searched for the two price pivots. Default 50.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=(
            "SELL when the just-confirmed price swing high exceeds the preceding swing "
            "high while n-period momentum (close minus close n bars ago), sampled at "
            "those exact price-pivot timestamps, is lower."
        ),
        source=(
            "John J. Murphy, Technical Analysis of the Financial Markets (1999), momentum "
            "oscillator and divergence; pivots follow Bill Williams' confirmed fractal "
            "construction: https://www.tradingview.com/support/solutions/43000591663-williams-fractal-indicator/"
        ),
        execution_limitations=(
            "The signal waits strength completed right-hand bars, so no future "
            "observation is read at the historical signal timestamp.",
            "Momentum is compared only at the two price pivots, not at unrelated "
            "oscillator maxima; the entire calculation window must be adjacent.",
            "Finite close-difference momentum; advisory long exit only.",
        ),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        values = cls.spec.parameter_schema.validate(params)
        period, lookback = values["momentum_period"], values["lookback"]
        assert isinstance(period, int) and isinstance(lookback, int)
        return period + lookback

    def evaluate_trigger(
        self, context: MarketContext, params: Mapping[str, object], state: Mapping[str, object]
    ) -> StrategyEvaluation:
        values = self.spec.parameter_schema.validate(params)
        period, strength, lookback = (
            values["momentum_period"],
            values["strength"],
            values["lookback"],
        )
        assert isinstance(period, int) and isinstance(strength, int) and isinstance(lookback, int)
        require_param(lookback >= 4 * strength + 2, "lookback >= 4 * strength + 2", "lookback")
        need = period + lookback
        bars = closed_bars(self.spec, context, need)
        if bars is None:
            return insufficient(self.spec, context)
        bars = bars[-need:]
        reason = gate(self.spec, context, state)
        if reason is not None:
            return blocked(self.spec, context, reason)
        if any(not adjacent(a, b) for a, b in zip(bars, bars[1:], strict=False)):
            return insufficient(self.spec, context)
        structure = bars[-lookback:]
        highs, _ = swing_pivots(structure, strength)
        triggered = False
        if len(highs) >= 2 and highs[-1][0] == len(structure) - 1 - strength:
            offset = len(bars) - lookback
            first, second = highs[-2][0], highs[-1][0]
            first_index, second_index = offset + first, offset + second
            first_momentum = bars[first_index].close - bars[first_index - period].close
            second_momentum = bars[second_index].close - bars[second_index - period].close
            triggered = highs[-1][1] > highs[-2][1] and second_momentum < first_momentum
        return finish(self.spec, context, bars[-1], state, triggered)
