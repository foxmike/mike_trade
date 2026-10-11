"""COVER when the close returns above the lower Keltner Channel."""

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
    known,
    parts,
)
from mike_trade.strategies.indicators.extended import KeltnerChannelCalculator

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "cover_keltner_channel_reversion"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


def _required_bars(validated: Mapping[str, object]) -> int:
    ema_period, atr_period = validated["ema_period"], validated["atr_period"]
    assert isinstance(ema_period, int) and isinstance(atr_period, int)
    return max(ema_period, atr_period) + 1


class CoverKeltnerChannelReversion(CoverStrategy):
    """COVER when a close below the lower Keltner band is followed by a close back above it.

    Channel (Raschke's modification): ``EMA(close, ema_period) -/+ multiplier * ATR(atr_period)``
    with Wilder ATR (shared ``KeltnerChannelCalculator``).  Each close is compared with the band
    computed on that bar (prior bar ``t-1`` and latest completed bar ``t``)::

        close[t-1] < lower[t-1]  AND  close[t] > lower[t]  ->  COVER

    Unlike keltner_channel_breakout (an upper-band momentum entry) this fades a lower-band
    excursion.  EMA and ATR are recursive and resumed from the strategy's persisted
    ``indicator_state`` bar by bar; absent, foreign-parameter or corrupt state is re-seeded from
    the supplied history.  Needs ``max(ema_period, atr_period) + 1`` completed bars (21 by
    default).
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
                    "ema_period", ParameterKind.INT, 20, 2, 200,
                    description="Centre-line EMA length. Default 20.",
                ),
                ParameterSpec(
                    "atr_period", ParameterKind.INT, 10, 1, 100,
                    description="Wilder ATR length for the band width. Default 10.",
                ),
                ParameterSpec(
                    "multiplier", ParameterKind.DECIMAL, Decimal(2), Decimal("0.5"), Decimal(10),
                    description="ATR multiple of the band offset. Default 2.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_INDICATOR),
        methodology=(
            "COVER when the prior completed close was strictly below the lower Keltner band "
            "and the latest completed close is strictly back above it. Rule definition only; "
            "no alpha or profitability claim."
        ),
        source=(
            "Chester Keltner, How to Make Money in Commodities (1960), as modified by Linda "
            "Raschke (EMA centre line, ATR width); StockCharts ChartSchool, Keltner Channels "
            "(EMA 20, ATR 10, multiplier 2). Same channel as the keltner_channel_reversion BUY."
        ),
        execution_limitations=(
            "Exit signal only: the engine must evaluate it only while a managed short exists and decides the cover order, quantity and any follow-on action; it never implies a BUY.",
            "Re-entry above the lower band confirms the reversion; it does not predict a move to "
            "the centre line.",
            "Completed adjacent M1 bars; regular session only; stale data never signals.",
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
        ema_period, atr_period = validated["ema_period"], validated["atr_period"]
        multiplier = validated["multiplier"]
        assert isinstance(ema_period, int) and isinstance(atr_period, int)
        assert isinstance(multiplier, Decimal)

        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, _required_bars(validated))
        if bars is None:
            return insufficient(self.spec, context)
        adv = advance(
            state, bars,
            f"{STRATEGY_ID}|{STRATEGY_VERSION}|{ema_period}|{atr_period}|{multiplier}",
            fresh=lambda: KeltnerChannelCalculator(ema_period, atr_period, multiplier),
            restore=KeltnerChannelCalculator.restore,
            feed=lambda calc, bar: calc.update(bar),
        )
        if reason is not None:
            return blocked(self.spec, context, reason, adv.updates)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context, adv.updates)
        lower_prior, lower_latest = parts(adv.prior, 3)[0], parts(adv.latest, 3)[0]
        if not known(lower_prior, lower_latest):
            return insufficient(self.spec, context, adv.updates)
        triggered = bars[-2].close < lower_prior and bars[-1].close > lower_latest
        return finish(self.spec, context, bars[-1], state, triggered, adv.updates)
