"""SELL when rolling Chaikin Money Flow crosses below its zero line."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.strategies.base import ParameterKind, ParameterSchema, ParameterSpec, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_LAST_SIGNAL_BAR, TIMEFRAME, closed_bars, crossed_below, finish, gate, insufficient, known, same_session
from mike_trade.strategies.indicators.finite import chaikin_money_flow
from mike_trade.strategies.sell.base import SellRule

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "chaikin_money_flow_reversal"
STRATEGY_VERSION = "1.0.0"
DEFAULT_PERIOD = 20
MAX_PERIOD = 50
WARMUP_BARS = MAX_PERIOD + 1
ZERO = Decimal("0")

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID, version=STRATEGY_VERSION, side=StrategyRole.SELL,
    category="technical", sub_category="volume", reference_symbols=(),
    required_timeframes=(TIMEFRAME,), warmup_bars=WARMUP_BARS,
    completed_bars_only=True, requires_volume=True,
    trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
    parameter_schema=ParameterSchema((ParameterSpec("period", ParameterKind.INT,
        DEFAULT_PERIOD, 2, MAX_PERIOD),)),
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=("SELL when n-period Chaikin Money Flow crosses from at-or-above zero "
                 "to below zero. This is a rule definition, not an alpha or profitability claim."),
    source=("Marc Chaikin's CMF; StockCharts ChartSchool, Chaikin Money Flow: "
            "https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/chaikin-money-flow-cmf"),
    execution_limitations=("Advisory long exit only; zero-range bars contribute zero "
                           "money-flow multiplier.",
                           "Completed regular-session 1-minute bars only; crossing bars "
                           "must share a New York session date."),
    reason_codes=REASON_CODES,
)


class ChaikinMoneyFlowReversalSell(SellRule):
    spec = _SPEC

    def evaluate_trigger(self, context: MarketContext, params: Mapping[str, object],
                         state: Mapping[str, object]) -> StrategyEvaluation:
        p = self.spec.parameter_schema.validate(params)["period"]
        assert isinstance(p, int)
        early = gate(self.spec, context, state)
        if early is not None:
            return early
        bars = closed_bars(context, p + 1)
        if bars is None or not same_session(bars[-2], bars[-1]):
            return insufficient(self.spec, context)
        prior = chaikin_money_flow(bars[-(p + 1):-1])
        latest = chaikin_money_flow(bars[-p:])
        if not known(prior, latest):
            return insufficient(self.spec, context)
        return finish(self.spec, context, bars[-1], state,
                      crossed_below(prior, ZERO, latest, ZERO))
