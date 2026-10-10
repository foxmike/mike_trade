"""BUY on a completed close crossing above the upper Bollinger Band."""

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
    finish,
    gate,
    insufficient,
)
from mike_trade.strategies.indicators.finite import bollinger

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "bollinger_band_breakout"
STRATEGY_VERSION = "1.0.0"
#: Declared trigger and only required timeframe (see `_SPEC`).
TIMEFRAME = CandleTimeframe.M1
DEFAULT_PERIOD = 20
DEFAULT_DEVIATIONS = Decimal("2")
MAX_PERIOD = 50


def _required_bars(period: int) -> int:
    """Completed bars needed for a prior and a latest value."""
    return period + 1


#: Requirement at the default period; `required_bars(params)` gives the configured one.
WARMUP_BARS = _required_bars(DEFAULT_PERIOD)

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID, version=STRATEGY_VERSION, side=StrategyRole.BUY,
    category="technical", sub_category="breakout", reference_symbols=(),
    required_timeframes=(TIMEFRAME,), warmup_bars=WARMUP_BARS,
    completed_bars_only=True, requires_volume=False,
    trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
    parameter_schema=ParameterSchema((
        ParameterSpec("period", ParameterKind.INT, DEFAULT_PERIOD, 2, MAX_PERIOD),
        ParameterSpec("deviations", ParameterKind.DECIMAL, DEFAULT_DEVIATIONS,
                      Decimal("0.1"), Decimal("5")),
    )), persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=("BUY when the completed close crosses from at-or-below to above the "
                 "upper Bollinger Band: close[t-1] <= upper[t-1] and close[t] > upper[t]. "
                 "This documents a method, not an alpha or profitability claim."),
    source=("John Bollinger, Bollinger on Bollinger Bands; population standard deviation "
            "as documented at https://www.bollingerbands.com/bollinger-bands; band-break "
            "scan interpretation: StockCharts ChartSchool, Bollinger Bands: "
            "https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-overlays/bollinger-bands"),
    execution_limitations=("Completed regular-session 1-minute bars only; prior/latest "
                           "crossing bars must be adjacent completed bars (no missing bar).",
                           "Population standard deviation is used; no squeeze condition is added."),
    reason_codes=REASON_CODES,
)


class BollingerBandBreakoutBuy(BuyStrategy):
    """Close crossing above the upper band, using a finite trailing window."""

    spec = _SPEC

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        return _required_bars(cls.spec.parameter_schema.validate(params)["period"])  # type: ignore[arg-type]

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        p = self.spec.parameter_schema.validate(params)
        period, deviations = p["period"], p["deviations"]
        assert isinstance(period, int) and isinstance(deviations, Decimal)
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, _required_bars(period))
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context)
        prior, latest = bars[-(period + 1):-1], bars[-period:]
        upper_prev = bollinger([b.close for b in prior], period, deviations)[2]
        upper_now = bollinger([b.close for b in latest], period, deviations)[2]
        return finish(self.spec, context, bars[-1], state,
                      bars[-2].close <= upper_prev and bars[-1].close > upper_now)
