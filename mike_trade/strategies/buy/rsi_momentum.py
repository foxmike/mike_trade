"""Long entry on Cardwell RSI positive reversal in an established trend."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import (
    BuyStrategy,
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
from mike_trade.strategies.indicators.core import rsi
from mike_trade.strategies.indicators.windows import swing_pivots

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "rsi_momentum"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


class RsiMomentumBuy(BuyStrategy):
    """BUY on Andrew Cardwell's RSI positive reversal within an established uptrend.

    Cardwell / Brown positive reversal: price forms a higher swing low (fractal low)
    while RSI at those exact swing-low timestamps forms a lower low, remaining within
    the normal bullish support zone (typically 30-55).  This indicates that despite
    a temporary oscillator dip, price held higher support, signaling strong underlying
    accumulation and trend continuation.
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version=STRATEGY_VERSION,
        side=StrategyRole.BUY,
        category="technical",
        sub_category="momentum",
        reference_symbols=(),
        required_timeframes=(TIMEFRAME,),
        warmup_bars=66,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec(
                    "rsi_period",
                    ParameterKind.INT,
                    14,
                    2,
                    50,
                    description="Wilder RSI lookback period. Default 14.",
                ),
                ParameterSpec(
                    "strength",
                    ParameterKind.INT,
                    2,
                    1,
                    5,
                    description="Completed bars on each side of a confirmed swing low. Default 2.",
                ),
                ParameterSpec(
                    "lookback",
                    ParameterKind.INT,
                    50,
                    10,
                    200,
                    description="Bars searched for the two swing lows. Default 50.",
                ),
                ParameterSpec(
                    "max_rsi_low",
                    ParameterKind.DECIMAL,
                    Decimal("55"),
                    Decimal("30"),
                    Decimal("70"),
                    description=(
                        "Maximum RSI value at the second swing low (bull zone). Default 55."
                    ),
                ),
                ParameterSpec(
                    "min_rsi_low",
                    ParameterKind.DECIMAL,
                    Decimal("30"),
                    Decimal("15"),
                    Decimal("50"),
                    description="Minimum RSI value at the second swing low. Default 30.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=(
            "BUY on Andrew Cardwell's RSI positive reversal: price forms a higher confirmed "
            "swing low while Wilder RSI at those exact pivot timestamps forms a lower low "
            "in the normal bull range (min_rsi_low <= RSI <= max_rsi_low)."
        ),
        source=(
            "Andrew Cardwell, Cardwell RSI EDGE; Constance Brown, Technical Analysis for the "
            "Trading Professional (McGraw-Hill, 1999), ch. 2-3 (RSI range rules and reversals); "
            "John J. Murphy, Technical Analysis of the Financial Markets (1999), ch. 10. "
            "Pivots follow Bill Williams' confirmed fractal construction."
        ),
        execution_limitations=(
            "The signal waits strength completed right-hand bars, so no future observation "
            "is read at the historical signal timestamp.",
            "RSI is sampled strictly at the two confirmed price swing lows, not at arbitrary "
            "oscillator extremes; the entire calculation window must be adjacent.",
            "Advisory long entry only; regular session only.",
        ),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        values = cls.spec.parameter_schema.validate(params)
        period, lookback = values["rsi_period"], values["lookback"]
        assert isinstance(period, int) and isinstance(lookback, int)
        return period + lookback + 2

    def evaluate(
        self, context: MarketContext, params: Mapping[str, object], state: Mapping[str, object]
    ) -> StrategyEvaluation:
        values = self.spec.parameter_schema.validate(params)
        period, strength, lookback = (
            values["rsi_period"],
            values["strength"],
            values["lookback"],
        )
        max_rsi, min_rsi = values["max_rsi_low"], values["min_rsi_low"]
        assert isinstance(period, int) and isinstance(strength, int) and isinstance(lookback, int)
        assert isinstance(max_rsi, Decimal) and isinstance(min_rsi, Decimal)

        require_param(lookback >= 4 * strength + 2, "lookback >= 4 * strength + 2", "lookback")
        require_param(min_rsi <= max_rsi, "min_rsi_low <= max_rsi_low", "max_rsi_low")

        need = period + lookback + 2
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
        _, lows = swing_pivots(structure, strength)
        triggered = False

        if len(lows) >= 2 and lows[-1][0] == len(structure) - 1 - strength:
            first_idx, first_price = lows[-2]
            second_idx, second_price = lows[-1]

            offset = len(bars) - lookback
            rsi_series = rsi([b.close for b in bars], period)

            first_rsi = rsi_series[offset + first_idx]
            second_rsi = rsi_series[offset + second_idx]

            if first_rsi is not None and second_rsi is not None:
                triggered = (
                    second_price > first_price
                    and second_rsi < first_rsi
                    and min_rsi <= second_rsi <= max_rsi
                )

        return finish(self.spec, context, bars[-1], state, triggered)
