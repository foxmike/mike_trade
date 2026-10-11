"""Long entry when a Bollinger-inside-Keltner squeeze releases with positive momentum."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import BuyStrategy, ParameterKind, ParameterSchema, ParameterSpec, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_LAST_SIGNAL_BAR, adjacent, blocked, closed_bars, finish, gate, insufficient
from mike_trade.strategies.indicators.windows import squeeze_momentum, squeeze_on

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "volatility_squeeze_breakout"
TIMEFRAME = CandleTimeframe.M1


class VolatilitySqueezeBreakoutBuy(BuyStrategy):
    """BUY on the bar where the squeeze turns off and the squeeze momentum is positive.

    John Carter's TTM-style squeeze: ``on`` when the Bollinger Bands (SMA +/- 2 population SD)
    lie inside the Keltner Channel (SMA +/- 1.5 x SMA of true range).  Signal at ``t``: squeeze
    on at ``t-1``, off at ``t``, and momentum ``> 0`` where momentum is the linear-regression
    value of ``close - mean(mid-range, SMA)`` over `period` bars.  Needs ``2 * period - 1``
    completed bars (39 by default).  Finite windows only; no recursive state.
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID, version="1.0.0", side=StrategyRole.BUY,
        category="technical", sub_category="breakout", reference_symbols=(),
        required_timeframes=(TIMEFRAME,), warmup_bars=39,
        completed_bars_only=True, requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema((
            ParameterSpec("period", ParameterKind.INT, 20, 5, 100,
                          description="Bollinger, Keltner and momentum length. Default 20."),
            ParameterSpec("bb_deviations", ParameterKind.DECIMAL, Decimal(2), Decimal("0.5"), Decimal(5),
                          description="Bollinger width in population standard deviations. Default 2."),
            ParameterSpec("kc_multiplier", ParameterKind.DECIMAL, Decimal("1.5"), Decimal("0.5"), Decimal(5),
                          description="Keltner width in average true ranges. Default 1.5."),
        )),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=("BUY when the Bollinger-inside-Keltner squeeze turns off on the completed bar "
                     "after being on, and squeeze momentum is above zero."),
        source=("John Carter, Mastering the Trade (2005), the TTM Squeeze; StockCharts ChartSchool, "
                "TTM Squeeze (Bollinger 20/2 inside Keltner 20/1.5; linear-regression momentum): https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/ttm-squeeze"),
        execution_limitations=("Keltner uses SMA +/- multiple of SMA(true range), the common public "
                               "TTM implementation, not the EMA/ATR channel.",
                               "Windows span the supplied completed bars like a continuous chart; "
                               "the two squeeze states must come from adjacent bars. "
                               "Regular session and BUY eligibility apply."),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        period = cls.spec.parameter_schema.validate(params)["period"]
        assert isinstance(period, int)
        return 2 * period - 1

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        v = self.spec.parameter_schema.validate(params)
        period, deviations, multiplier = v["period"], v["bb_deviations"], v["kc_multiplier"]
        assert isinstance(period, int)
        assert isinstance(deviations, Decimal) and isinstance(multiplier, Decimal)
        need = 2 * period - 1
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, need)
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context)
        was_on = squeeze_on(bars[-period - 2:-1], period, deviations, multiplier)
        is_on = squeeze_on(bars[-period - 1:], period, deviations, multiplier)
        triggered = (was_on and not is_on
                     and squeeze_momentum(bars[-need:], period) > 0)
        return finish(self.spec, context, bars[-1], state, triggered)
