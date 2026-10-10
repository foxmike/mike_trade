"""BUY when rolling Chaikin Money Flow crosses above its zero line."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import BuyStrategy, ParameterKind, ParameterSchema, ParameterSpec, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import (
    REASON_CODES,
    STATE_LAST_SIGNAL_BAR,
    adjacent,
    blocked,
    closed_bars,
    crossed_above,
    finish,
    gate,
    insufficient,
    known,
)
from mike_trade.strategies.indicators.finite import chaikin_money_flow

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "chaikin_money_flow"
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
ZERO = Decimal("0")

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID, version=STRATEGY_VERSION, side=StrategyRole.BUY,
    category="technical", sub_category="volume", reference_symbols=(),
    required_timeframes=(TIMEFRAME,), warmup_bars=WARMUP_BARS,
    completed_bars_only=True, requires_volume=True,
    trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
    parameter_schema=ParameterSchema((ParameterSpec("period", ParameterKind.INT,
        DEFAULT_PERIOD, 2, MAX_PERIOD),)),
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=("BUY when n-period Chaikin Money Flow crosses from at-or-below zero "
                 "to above zero. This is a rule definition, not an alpha or profitability claim."),
    source=("Marc Chaikin's CMF; StockCharts ChartSchool, Chaikin Money Flow: "
            "https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/chaikin-money-flow-cmf"),
    execution_limitations=("Rolling money-flow-volume divided by rolling volume; a "
                           "zero high-low range contributes zero multiplier.",
                           "Completed regular-session 1-minute bars only; crossing bars "
                           "must be adjacent completed bars (no missing bar)."),
    reason_codes=REASON_CODES,
)


class ChaikinMoneyFlowBuy(BuyStrategy):
    spec = _SPEC

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        return _required_bars(cls.spec.parameter_schema.validate(params)["period"])  # type: ignore[arg-type]

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
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
        prior = chaikin_money_flow(bars[-(p + 1):-1])
        latest = chaikin_money_flow(bars[-p:])
        if not known(prior, latest):
            return insufficient(self.spec, context)
        return finish(self.spec, context, bars[-1], state,
                      crossed_above(prior, ZERO, latest, ZERO))
