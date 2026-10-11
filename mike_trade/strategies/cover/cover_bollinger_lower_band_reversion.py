"""COVER when a close back above the lower Bollinger Band follows a close below it."""

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

STRATEGY_ID = "cover_bollinger_lower_band_reversion"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


class CoverBollingerLowerBandReversion(CoverStrategy):
    """COVER when the close returns strictly above the lower Bollinger Band after a completed
    close strictly below it.

    Bands over the latest n closes: ``middle = SMA(n)``, ``lower = middle - k * SD`` with the
    population standard deviation (the shared ``bollinger`` helper).  Each bar is compared with
    the band computed *on that bar* (prior bar ``t-1`` and latest completed bar ``t``)::

        close[t-1] < lower[t-1]  AND  close[t] > lower[t]  ->  COVER

    A mean-reversion rule: a stretched-low close that has re-entered the band.  It is not the
    upper-band breakout of bollinger_band_breakout and not the mirror of the
    bollinger_upper_band_reversal SELL's long exit.  Finite window, so no indicator state is
    persisted.  Needs ``period + 1`` completed bars (21 by default).
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version=STRATEGY_VERSION,
        side=StrategyRole.COVER,
        category="technical",
        sub_category="mean_reversion",
        reference_symbols=(),
        required_timeframes=(TIMEFRAME,),
        warmup_bars=21,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec(
                    "period", ParameterKind.INT, 20, 2, 200,
                    description="Bollinger SMA/standard-deviation length of closes. Default 20.",
                ),
                ParameterSpec(
                    "deviations", ParameterKind.DECIMAL, Decimal(2), Decimal("0.1"), Decimal(5),
                    description="Standard-deviation multiple of the band offset. Default 2.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=(
            "COVER when the prior completed close was strictly below its lower Bollinger Band "
            "and the latest completed close is strictly back above its lower band. Rule "
            "definition only; no alpha or profitability claim."
        ),
        source=(
            "John Bollinger, Bollinger on Bollinger Bands (20-period SMA, 2 standard "
            "deviations); official band formula: https://www.bollingerbands.com/bollinger-bands; "
            "same band calculation as the bollinger_upper_band_reversal SELL."
        ),
        execution_limitations=(
            "Exit signal only: the engine must evaluate it only while a managed short exists and decides the cover order, quantity and any follow-on action; it never implies a BUY.",
            "A band tag alone is not a signal: a completed close below the band followed by a "
            "completed close back above it is required.",
            "Population standard deviation; adjacent completed regular-session M1 bars only; a "
            "zero-width band (flat window) cannot be crossed strictly.",
        ),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        period = cls.spec.parameter_schema.validate(params)["period"]
        assert isinstance(period, int)
        return period + 1

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        validated = self.spec.parameter_schema.validate(params)
        period, deviations = validated["period"], validated["deviations"]
        assert isinstance(period, int) and isinstance(deviations, Decimal)

        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, period + 1)
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context)
        lower_prior = bollinger([bar.close for bar in bars[-period - 1 : -1]], period, deviations)[0]
        lower_latest = bollinger([bar.close for bar in bars[-period:]], period, deviations)[0]
        triggered = bars[-2].close < lower_prior and bars[-1].close > lower_latest
        return finish(self.spec, context, bars[-1], state, triggered)
