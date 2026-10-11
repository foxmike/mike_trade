"""COVER when +DI crosses above -DI."""

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
    STATE_INDICATOR,
    STATE_LAST_SIGNAL_BAR,
    adjacent,
    advance,
    blocked,
    closed_bars,
    crossed_above,
    finish,
    gate,
    insufficient,
    known,
    parts,
)
from mike_trade.strategies.indicators.core import DMICalculator

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "cover_dmi_bullish_crossover"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


def _required_bars(validated: Mapping[str, object]) -> int:
    period = validated["period"]
    assert isinstance(period, int)
    return period + 2


class CoverDmiBullishCrossover(CoverStrategy):
    """COVER when +DI crosses above -DI on a completed bar.

    Wilder DMI: TR, +DM and -DM from the second bar; the first smoothed value is the sum of the
    first n raw values, then ``smoothed = previous - previous / n + current``;
    ``+DI = 100 * smoothed +DM / smoothed TR`` and likewise ``-DI``.  Signal (edge)::

        +DI[t-1] <= -DI[t-1]  AND  +DI[t] > -DI[t]  ->  COVER

    No ADX strength filter is applied.  The smoothed sums are recursive and resumed from the
    strategy's persisted ``indicator_state`` bar by bar; absent, foreign-parameter or corrupt
    state is re-seeded from the supplied history.  Needs ``period + 2`` completed bars (16 by
    default).
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version=STRATEGY_VERSION,
        side=StrategyRole.COVER,
        category="technical",
        sub_category="trend_following",
        reference_symbols=(),
        required_timeframes=(TIMEFRAME,),
        warmup_bars=16,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec(
                    "period", ParameterKind.INT, 14, 2, 100,
                    description="Wilder smoothing length in completed bars. Default 14.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_INDICATOR),
        methodology=(
            "COVER when +DI crosses above -DI on a completed bar (prior +DI <= -DI and latest "
            "+DI > -DI). Restates a published textbook definition; no alpha or profitability "
            "claim."
        ),
        source=(
            "J. Welles Wilder, 'New Concepts in Technical Trading Systems' (1978) as "
            "documented in StockCharts ChartSchool, 'Average Directional Index (ADX)' "
            "(14-period Wilder smoothing; +DI/-DI crossover). Same calculation as the "
            "dmi_crossover BUY."
        ),
        execution_limitations=(
            "Exit signal only: the engine must evaluate it only while a managed short exists and decides the cover order, quantity and any follow-on action; it never implies a BUY.",
            "Completed adjacent M1 bars; regular session only; stale data never signals.",
            "No ADX trend-strength filter: a +DI/-DI cross in a trendless market also signals.",
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
        adv = advance(
            state, bars, f"{STRATEGY_ID}|{STRATEGY_VERSION}|{period}",
            fresh=lambda: DMICalculator(period),
            restore=DMICalculator.restore,
            feed=lambda calc, bar: calc.update(bar),
        )
        if reason is not None:
            return blocked(self.spec, context, reason, adv.updates)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context, adv.updates)
        plus_prior, minus_prior = parts(adv.prior, 2)
        plus_latest, minus_latest = parts(adv.latest, 2)
        if not known(plus_prior, minus_prior, plus_latest, minus_latest):
            return insufficient(self.spec, context, adv.updates)
        triggered = crossed_above(plus_prior, minus_prior, plus_latest, minus_latest)
        return finish(self.spec, context, bars[-1], state, triggered, adv.updates)
