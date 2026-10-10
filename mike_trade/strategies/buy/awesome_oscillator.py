"""Long entry on a bullish Awesome Oscillator zero-line cross."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import BuyStrategy, ParameterSchema, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_LAST_SIGNAL_BAR, adjacent, blocked, closed_bars, crossed_above, finish, gate, insufficient
from mike_trade.strategies.indicators.finite import awesome_oscillator

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext


class AwesomeOscillatorBuy(BuyStrategy):
    spec = StrategySpec(
        strategy_id="awesome_oscillator", version="1.0.0", side=StrategyRole.BUY,
        category="technical", sub_category="oscillator", reference_symbols=(),
        required_timeframes=(CandleTimeframe.M1,), warmup_bars=35,
        completed_bars_only=True, requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=CandleTimeframe.M1,
        parameter_schema=ParameterSchema(()), persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology="BUY when AO = SMA5(median price) - SMA34(median price) crosses above zero.",
        source="TradingView Help, Awesome Oscillator (AO): https://www.tradingview.com/support/solutions/43000501826-awesome-oscillator-ao/",
        execution_limitations=("Uses Bill Williams's fixed 5/34 median-price periods.",
                               "Completed adjacent M1 crossing bars; regular session and BUY eligibility apply."),
        reason_codes=REASON_CODES,
    )

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        self.spec.parameter_schema.validate(params)
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, 35)
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context)
        prior = awesome_oscillator(bars[-35:-1])
        latest = awesome_oscillator(bars[-34:])
        return finish(self.spec, context, bars[-1], state,
                      crossed_above(prior, Decimal(0), latest, Decimal(0)))
