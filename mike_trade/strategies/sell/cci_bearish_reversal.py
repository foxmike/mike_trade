"""SELL when CCI returns below its conventional overbought boundary."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.strategies.base import ParameterKind, ParameterSchema, ParameterSpec, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_LAST_SIGNAL_BAR, TIMEFRAME, closed_bars, crossed_below, finish, gate, insufficient, known, same_session
from mike_trade.strategies.indicators.core import cci
from mike_trade.strategies.sell.base import SellRule

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "cci_bearish_reversal"
STRATEGY_VERSION = "1.0.0"
DEFAULT_PERIOD = 20
MAX_PERIOD = 50
WARMUP_BARS = MAX_PERIOD + 1
OVERBOUGHT = Decimal("100")

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID, version=STRATEGY_VERSION, side=StrategyRole.SELL,
    category="technical", sub_category="oscillator", reference_symbols=(),
    required_timeframes=(TIMEFRAME,), warmup_bars=WARMUP_BARS,
    completed_bars_only=True, requires_volume=False,
    trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
    parameter_schema=ParameterSchema((ParameterSpec("period", ParameterKind.INT,
        DEFAULT_PERIOD, 2, MAX_PERIOD),)),
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=("SELL when CCI crosses downward through +100: CCI[t-1] >= +100 "
                 "and CCI[t] < +100. This is a rule definition, not an alpha or "
                 "profitability claim."),
    source=("Donald Lambert, Commodity Channel Index; StockCharts ChartSchool, "
            "Commodity Channel Index (CCI): https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/commodity-channel-index-cci"),
    execution_limitations=("Advisory long exit only; position eligibility and arbitration "
                           "remain with SellRule composition.",
                           "Zero mean deviation is undefined; completed regular-session "
                           "1-minute bars only."),
    reason_codes=REASON_CODES,
)


class CciBearishReversalSell(SellRule):
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
        values = cci(bars[-(p + 1):], p)
        if not known(values[-2], values[-1]):
            return insufficient(self.spec, context)
        return finish(self.spec, context, bars[-1], state,
                      crossed_below(values[-2], OVERBOUGHT, values[-1], OVERBOUGHT))
