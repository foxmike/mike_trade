"""Long entry when the averaged Ease of Movement crosses above zero."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import BuyStrategy, ParameterKind, ParameterSchema, ParameterSpec, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_LAST_SIGNAL_BAR, adjacent, blocked, closed_bars, crossed_above, finish, gate, insufficient
from mike_trade.strategies.indicators.windows import ease_of_movement

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "ease_of_movement"
TIMEFRAME = CandleTimeframe.M1


class EaseOfMovementBuy(BuyStrategy):
    """BUY when the `period`-bar average of Arms' Ease of Movement crosses above zero.

    ``EMV = ((H+L)/2 - (H'+L')/2) / ((V / scale) / (H - L))`` per bar, averaged simply over
    `period` bars.  A positive average means price is rising on light volume relative to its
    range.  A zero-range bar contributes 0 (infinite box ratio); a zero-volume bar leaves EMV
    undefined and yields INSUFFICIENT_DATA.  The scale constant never changes the sign, so the
    zero-line crossing is scale-independent.  Needs ``period + 2`` completed bars (16 by
    default).  Finite windows only.
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID, version="1.0.0", side=StrategyRole.BUY,
        category="technical", sub_category="volume", reference_symbols=(),
        required_timeframes=(TIMEFRAME,), warmup_bars=16,
        completed_bars_only=True, requires_volume=True,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema((
            ParameterSpec("period", ParameterKind.INT, 14, 2, 100,
                          description="Simple-average length of one-bar EMV. Default 14."),
        )),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology="BUY when the n-bar simple average of Ease of Movement crosses from at-or-below to above zero.",
        source=("Richard Arms Jr., Ease of Movement (EMV); StockCharts ChartSchool, Ease of "
                "Movement (EMV): https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/ease-of-movement-emv"),
        execution_limitations=("Entry signal only: no stops, sizing, targets, or orders.",
                               "Completed adjacent M1 bars; regular session and BUY eligibility apply."),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        period = cls.spec.parameter_schema.validate(params)["period"]
        assert isinstance(period, int)
        return period + 2

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        period = self.spec.parameter_schema.validate(params)["period"]
        assert isinstance(period, int)
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, period + 2)
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context)
        prior = ease_of_movement(bars[-period - 2:-1], period)
        latest = ease_of_movement(bars[-period - 1:], period)
        if prior is None or latest is None:
            return insufficient(self.spec, context)
        zero = Decimal(0)
        return finish(self.spec, context, bars[-1], state,
                      crossed_above(prior, zero, latest, zero))
