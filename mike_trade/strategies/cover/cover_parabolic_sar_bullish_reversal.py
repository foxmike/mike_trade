"""COVER when the Parabolic SAR trend flips from downtrend to uptrend."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
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
    finish,
    gate,
    insufficient,
    require_param,
)
from mike_trade.strategies.indicators.core import ParabolicSARCalculator

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "cover_parabolic_sar_bullish_reversal"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


class CoverParabolicSarBullishReversal(CoverStrategy):
    """COVER on the completed bar where the Parabolic SAR trend changes down to up.

    ``SAR[t] = SAR[t-1] + AF * (EP - SAR[t-1])``; AF starts at ``step``, grows by ``step`` on each
    new extreme point and is capped at ``maximum``; in a downtrend SAR is never below the prior
    two highs.  A downtrend reverses when a bar's high exceeds the SAR (the shared
    ``ParabolicSARCalculator``: new SAR = prior EP, new EP = the penetrating bar's high, AF =
    step).  Signal (edge)::

        trend[t-1] = down  AND  trend[t] = up  ->  COVER

    SAR, EP, AF, trend and the last two highs/lows are recursive: they are resumed from the
    strategy's persisted ``indicator_state`` bar by bar (never recomputed from the bounded candle
    window, which would shift the SAR seed); absent, foreign-parameter or corrupt state is
    re-seeded from the supplied history.  Needs 3 completed bars.
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version=STRATEGY_VERSION,
        side=StrategyRole.COVER,
        category="technical",
        sub_category="trend_following",
        reference_symbols=(),
        required_timeframes=(TIMEFRAME,),
        warmup_bars=3,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec(
                    "step", ParameterKind.DECIMAL, Decimal("0.02"), Decimal("0.001"), Decimal("0.5"),
                    description="Acceleration factor start and increment. Default 0.02.",
                ),
                ParameterSpec(
                    "maximum", ParameterKind.DECIMAL, Decimal("0.20"), Decimal("0.001"), Decimal(1),
                    description="Acceleration factor cap; must be at least step. Default 0.20.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_INDICATOR),
        methodology=(
            "COVER on the completed bar where the Parabolic SAR trend flips from downtrend to "
            "uptrend (price penetrates the SAR from below). Restates a published textbook "
            "definition; no alpha or profitability claim."
        ),
        source=(
            "J. Welles Wilder, 'New Concepts in Technical Trading Systems' (1978) as "
            "documented in StockCharts ChartSchool, 'Parabolic SAR' (AF 0.02 step, 0.20 "
            "maximum; prior-two-bar clamp; reversal when price penetrates SAR). Same "
            "calculation as the parabolic_sar BUY."
        ),
        execution_limitations=(
            "Exit signal only: the engine must evaluate it only while a managed short exists and decides the cover order, quantity and any follow-on action; it never implies a BUY.",
            "Completed adjacent M1 bars; regular session only; stale data never signals.",
            "The initial trend is chosen from the first two closes (the shared calculator's "
            "convention), so a flip within the first bars after a cold start can reflect that "
            "seed; the SAR is not used as a stop.",
        ),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        cls.spec.parameter_schema.validate(params)
        return 3

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        validated = self.spec.parameter_schema.validate(params)
        step, maximum = validated["step"], validated["maximum"]
        assert isinstance(step, Decimal) and isinstance(maximum, Decimal)
        require_param(maximum >= step, "maximum >= step", "maximum")

        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, 3)
        if bars is None:
            return insufficient(self.spec, context)
        adv = advance(
            state, bars, f"{STRATEGY_ID}|{STRATEGY_VERSION}|{step}|{maximum}",
            fresh=lambda: ParabolicSARCalculator(step, maximum),
            restore=ParabolicSARCalculator.restore,
            feed=lambda calc, bar: calc.update(bar),
        )
        if reason is not None:
            return blocked(self.spec, context, reason, adv.updates)
        if not adjacent(bars[-2], bars[-1]) or adv.prior is None or adv.latest is None:
            return insufficient(self.spec, context, adv.updates)
        triggered = not adv.prior[1] and adv.latest[1]
        return finish(self.spec, context, bars[-1], state, triggered, adv.updates)
