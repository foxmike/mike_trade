"""COVER when Aroon-Up crosses above Aroon-Down after a bearish reading."""

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
    known,
)
from mike_trade.strategies.indicators.core import aroon

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "cover_aroon_bullish_reversal"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


def _required_bars(validated: Mapping[str, object]) -> int:
    period = validated["period"]
    assert isinstance(period, int)
    return period + 2


class CoverAroonBullishReversal(CoverStrategy):
    """COVER when Aroon-Up crosses above Aroon-Down on a bar whose predecessor was bearish.

    ``Aroon-Up = 100 * (n - bars since highest high) / n`` and ``Aroon-Down = 100 * (n - bars
    since lowest low) / n`` over the latest n + 1 bars (ties resolve to the most recent bar; the
    shared ``aroon`` helper).  Signal (edge, prior bar ``t-1`` and latest completed bar ``t``)::

        Down[t-1] > Up[t-1]  AND  Up[t] > Down[t]  ->  COVER

    "After bearish conditions" is made deterministic as a strictly bearish prior bar
    (Aroon-Down above Aroon-Up): a tie on the prior bar is neutral, not bearish, and does not
    qualify (the aroon_trend BUY accepts ``Up[t-1] <= Down[t-1]``).  Finite window, so no
    indicator state is persisted.  Needs ``period + 2`` completed bars (27 by default).
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version=STRATEGY_VERSION,
        side=StrategyRole.COVER,
        category="technical",
        sub_category="trend_following",
        reference_symbols=(),
        required_timeframes=(TIMEFRAME,),
        warmup_bars=27,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec(
                    "period", ParameterKind.INT, 25, 2, 200,
                    description="Aroon look-back in completed bars. Default 25.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=(
            "COVER on the completed bar where Aroon-Up crosses above Aroon-Down after the "
            "prior bar had Aroon-Down strictly above Aroon-Up (bearish to bullish). Restates "
            "a published textbook definition; no alpha or profitability claim."
        ),
        source=(
            "Tushar Chande's Aroon (1995) as documented in StockCharts ChartSchool, 'Aroon' "
            "(25 periods; Aroon-Up crossing above Aroon-Down is the first stage of an "
            "uptrend). Same calculation as the aroon_trend BUY; the strictly bearish prior bar "
            "is this strategy's documented definition of 'after bearish conditions'."
        ),
        execution_limitations=(
            "Exit signal only: the engine must evaluate it only while a managed short exists and decides the cover order, quantity and any follow-on action; it never implies a BUY.",
            "Completed adjacent M1 bars; regular session only; stale data never signals.",
            "A bearish-to-neutral-to-bullish path (a tied bar in between) does not signal; the "
            "50/70/100 Aroon strength levels are not used.",
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
        bars = closed_bars(self.spec, context, _required_bars(validated))
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context)
        up, down = aroon(bars[-(period + 2):], period)
        if not known(up[-2], up[-1], down[-2], down[-1]):
            return insufficient(self.spec, context)
        triggered = down[-2] > up[-2] and up[-1] > down[-1]
        return finish(self.spec, context, bars[-1], state, triggered)
