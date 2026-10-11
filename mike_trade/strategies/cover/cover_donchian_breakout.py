"""COVER when the close breaks above the Donchian upper channel."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import (
    CoverStrategy,
    ParameterKind,
    ParameterSchema,
    ParameterSpec,
    StrategyEvaluation,
    StrategySpec,
    TriggerKind,
)
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
from mike_trade.strategies.indicators.core import donchian

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "cover_donchian_breakout"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


def _required_bars(validated: Mapping[str, object]) -> int:
    period = validated["period"]
    assert isinstance(period, int)
    return period + 1


class CoverDonchianBreakout(CoverStrategy):
    """COVER while the latest completed close is above the highest high of the preceding `period` bars.

    ``Upper[t]`` = highest high of bars ``t-n .. t-1``: the signal bar is
    excluded from the reference window.  Signal (level on the latest completed bar)::

        close[t] > Upper[t]  ->  COVER

    The reference window and the signal bar must be ``period + 1`` consecutive completed bars:
    a missing candle inside the window leaves the extreme unknown, so the result is
    INSUFFICIENT_DATA and nothing is filled.  No recursive state is kept (``last_signal_bar``
    only).  Needs ``period + 1`` completed bars (21 by default).
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version=STRATEGY_VERSION,
        side=StrategyRole.COVER,
        category="technical",
        sub_category="breakout",
        reference_symbols=(),
        required_timeframes=(TIMEFRAME,),
        warmup_bars=21,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.LEVEL_ON_CLOSED_BAR,
        trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec(
                    "period", ParameterKind.INT, 20, 2, 400,
                    description="Channel look-back in completed bars, signal bar excluded. Default 20.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=(
            "COVER when the latest completed close is above the highest high of the preceding "
            "`period` bars. Rule definition only; no alpha or profitability claim."
        ),
        source=(
            "Richard Donchian's price channel as documented in StockCharts ChartSchool, 'Price "
            "Channels' (20 default; the channel uses prices prior to the current period)."
        ),
        execution_limitations=(
            "Exit signal only: the engine must evaluate it only while a managed short exists and decides the cover order, quantity and any follow-on action; it never implies a BUY.",
            "The close, not an intraday touch, is compared; every bar of the window must be consecutive.",
            "Regular session only; stale data never signals.",
        ),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        return _required_bars(cls.spec.parameter_schema.validate(params))

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        validated = self.spec.parameter_schema.validate(params)
        period = validated["period"]
        assert isinstance(period, int)
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, period + 1)
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        window = bars[-(period + 1):]
        if any(not adjacent(a, b) for a, b in zip(window, window[1:])):
            return insufficient(self.spec, context)
        channel, _ = donchian(window, period)
        level = channel[-1]
        if level is None:
            return insufficient(self.spec, context)
        return finish(self.spec, context, bars[-1], state, window[-1].close > level)
