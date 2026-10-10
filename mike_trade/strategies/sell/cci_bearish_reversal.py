"""SELL when CCI returns below its conventional overbought boundary."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import ParameterKind, ParameterSchema, ParameterSpec, StrategyEvaluation, StrategySpec, TriggerKind
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
)
from mike_trade.strategies.indicators.core import cci
from mike_trade.strategies.sell.base import SellRule

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "cci_bearish_reversal"
STRATEGY_VERSION = "1.0.0"
#: Declared trigger and only required timeframe (see `_SPEC`).
TIMEFRAME = CandleTimeframe.M1
DEFAULT_PERIOD = 20
MAX_PERIOD = 50


def _required_bars(period: int) -> int:
    """Completed bars needed for a prior and a latest value."""
    return period + 1


#: Requirement at the default period; `required_bars(params)` gives the configured one.
WARMUP_BARS = _required_bars(DEFAULT_PERIOD)
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

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        return _required_bars(cls.spec.parameter_schema.validate(params)["period"])  # type: ignore[arg-type]

    def evaluate_trigger(self, context: MarketContext, params: Mapping[str, object],
                         state: Mapping[str, object]) -> StrategyEvaluation:
        p = self.spec.parameter_schema.validate(params)["period"]
        assert isinstance(p, int)
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, _required_bars(p))
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context)
        values = cci(bars[-(p + 1):], p)
        if not known(values[-2], values[-1]):
            return insufficient(self.spec, context)
        return finish(self.spec, context, bars[-1], state,
                      crossed_below(values[-2], OVERBOUGHT, values[-1], OVERBOUGHT))
