"""Long exit when price closes below the latest confirmed five-bar swing low."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import ParameterKind, ParameterSchema, ParameterSpec, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_LAST_SIGNAL_BAR, adjacent, blocked, closed_bars, finish, gate, insufficient
from mike_trade.strategies.sell.base import SellRule

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext


class PreviousSwingLowBreakSell(SellRule):
    spec = StrategySpec(
        strategy_id="previous_swing_low_break", version="1.0.0", side=StrategyRole.SELL,
        category="technical", sub_category="price_action", reference_symbols=(),
        required_timeframes=(CandleTimeframe.M1,), warmup_bars=21,
        completed_bars_only=True, requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=CandleTimeframe.M1,
        parameter_schema=ParameterSchema((ParameterSpec("lookback", ParameterKind.INT, 20, 5, 100),)),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology="SELL when close crosses below the most recent confirmed five-bar fractal low in the prior lookback bars.",
        source="Bill Williams, Trading Chaos; TradingView Help, Williams Fractal indicator: https://www.tradingview.com/support/solutions/43000591663-williams-fractal-indicator/",
        execution_limitations=("The pivot needs two completed bars on each side; no future bar is read.",
                               "Pivot's five bars and the two crossing bars must each be adjacent; advisory long exit through SellRule."),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        lookback = cls.spec.parameter_schema.validate(params)["lookback"]
        assert isinstance(lookback, int)
        return lookback + 1

    def evaluate_trigger(self, context: MarketContext, params: Mapping[str, object],
                         state: Mapping[str, object]) -> StrategyEvaluation:
        lookback = self.spec.parameter_schema.validate(params)["lookback"]
        assert isinstance(lookback, int)
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, lookback + 1)
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context)
        prior = bars[-lookback - 1:-1]
        pivot_low = None
        for center in range(len(prior) - 3, 1, -1):
            group = prior[center - 2:center + 3]
            if any(not adjacent(a, b) for a, b in zip(group, group[1:])):
                continue
            if prior[center].low < min(bar.low for i, bar in enumerate(group) if i != 2):
                pivot_low = prior[center].low
                break
        triggered = (pivot_low is not None and bars[-2].close >= pivot_low
                     and bars[-1].close < pivot_low)
        return finish(self.spec, context, bars[-1], state, triggered)
