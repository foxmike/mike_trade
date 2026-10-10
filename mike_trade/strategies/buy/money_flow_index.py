"""BUY when finite-window Money Flow Index recovers above 20."""

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
from mike_trade.strategies.indicators.finite import money_flow_index

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "money_flow_index"
STRATEGY_VERSION = "1.0.0"
#: Declared trigger and only required timeframe (see `_SPEC`).
TIMEFRAME = CandleTimeframe.M1
DEFAULT_PERIOD = 14
MAX_PERIOD = 50


def _required_bars(period: int) -> int:
    """Completed bars needed for a prior and a latest value."""
    return period + 2


#: Requirement at the default period; `required_bars(params)` gives the configured one.
WARMUP_BARS = _required_bars(DEFAULT_PERIOD)
OVERSOLD = Decimal("20")

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID, version=STRATEGY_VERSION, side=StrategyRole.BUY,
    category="technical", sub_category="volume", reference_symbols=(),
    required_timeframes=(TIMEFRAME,), warmup_bars=WARMUP_BARS,
    completed_bars_only=True, requires_volume=True,
    trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
    parameter_schema=ParameterSchema((ParameterSpec("period", ParameterKind.INT,
        DEFAULT_PERIOD, 2, MAX_PERIOD),)),
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=("BUY when the n-period MFI moves from at-or-below 20 to above 20; "
                 "MFI uses typical-price volume assigned positive/negative by the "
                 "direction of the typical-price change. This is a rule definition, "
                 "not an alpha or profitability claim."),
    source=("StockCharts ChartSchool, Money Flow Index (MFI): "
            "https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/money-flow-index-mfi"),
    execution_limitations=("Finite rolling flow sums; equal typical prices contribute "
                           "to neither side; no signed flow yields undefined MFI.",
                           "Completed regular-session 1-minute bars only; crossing bars "
                           "must be adjacent completed bars (no missing bar)."),
    reason_codes=REASON_CODES,
)


class MoneyFlowIndexBuy(BuyStrategy):
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
        prior = money_flow_index(bars[-(p + 2):-1], p)
        latest = money_flow_index(bars[-(p + 1):], p)
        if not known(prior, latest):
            return insufficient(self.spec, context)
        return finish(self.spec, context, bars[-1], state,
                      crossed_above(prior, OVERSOLD, latest, OVERSOLD))
