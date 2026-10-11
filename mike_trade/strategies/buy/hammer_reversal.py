"""Long entry on the confirmation bar after a hammer that ends a decline."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import BuyStrategy, ParameterKind, ParameterSchema, ParameterSpec, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_LAST_SIGNAL_BAR, adjacent, blocked, closed_bars, finish, gate, insufficient
from mike_trade.strategies.indicators.windows import is_hammer

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "hammer_reversal"
TIMEFRAME = CandleTimeframe.M1


class HammerReversalBuy(BuyStrategy):
    """BUY when a bar closes above the high of the immediately preceding confirmed-shape hammer.

    Hammer (bar ``h = t-1``): lower shadow ``>= shadow_ratio`` x real body (Nison: at least twice),
    upper shadow ``<= max_upper_shadow`` x range (negligible), non-zero range.  Prior decline:
    ``close[h-1] < close[h-1-trend_bars]``.  Nison requires bullish confirmation, so the signal is
    the next bar: ``close[t] > high[h]``.  Needs ``trend_bars + 3`` completed bars (8 by default).
    Finite windows only.
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID, version="1.0.0", side=StrategyRole.BUY,
        category="technical", sub_category="price_action", reference_symbols=(),
        required_timeframes=(TIMEFRAME,), warmup_bars=8,
        completed_bars_only=True, requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema((
            ParameterSpec("trend_bars", ParameterKind.INT, 5, 2, 50,
                          description="Bars over which the pre-hammer close must have fallen. Default 5."),
            ParameterSpec("shadow_ratio", ParameterKind.DECIMAL, Decimal(2), Decimal(1), Decimal(10),
                          description="Minimum lower shadow as a multiple of the real body. Default 2."),
            ParameterSpec("max_upper_shadow", ParameterKind.DECIMAL, Decimal("0.1"), Decimal(0), Decimal("0.5"),
                          description="Maximum upper shadow as a fraction of the bar's range. Default 0.1."),
        )),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=("BUY on the bar after a hammer (long lower shadow, small body at the top) that "
                     "follows a decline, when that bar closes above the hammer's high."),
        source=("Steve Nison, Japanese Candlestick Charting Techniques (2nd ed., 2001): hammer "
                "(lower shadow at least twice the body, little or no upper shadow, after a "
                "decline) and the need for bullish confirmation; shadow thresholds quantified "
                "as in TA-Lib's CDLHAMMER."),
        execution_limitations=("Entry signal only: no stops, sizing, targets, or orders.",
                               "Nison's daily-bar candle adapted to M1 bars; confirmation is the "
                               "close above the hammer's high (an objective reading of 'higher').",
                               "The hammer, its predecessor and the confirmation bar must be "
                               "adjacent completed bars; regular session and BUY eligibility apply."),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        trend = cls.spec.parameter_schema.validate(params)["trend_bars"]
        assert isinstance(trend, int)
        return trend + 3

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        v = self.spec.parameter_schema.validate(params)
        trend, ratio, upper = v["trend_bars"], v["shadow_ratio"], v["max_upper_shadow"]
        assert isinstance(trend, int) and isinstance(ratio, Decimal) and isinstance(upper, Decimal)
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, trend + 3)
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        before, hammer, latest = bars[-3], bars[-2], bars[-1]
        if not (adjacent(before, hammer) and adjacent(hammer, latest)):
            return insufficient(self.spec, context)
        triggered = (is_hammer(hammer, ratio, upper) and before.close < bars[-3 - trend].close
                     and latest.close > hammer.high)
        return finish(self.spec, context, latest, state, triggered)
