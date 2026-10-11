"""COVER when Supertrend flips from downtrend to uptrend."""

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
    parts,
)
from mike_trade.strategies.indicators.extended import SupertrendCalculator

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "cover_supertrend_bullish_flip"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


class CoverSupertrendBullishFlip(CoverStrategy):
    """COVER on the completed bar where the Supertrend direction changes down to up.

    Bands are ``(high + low) / 2 +/- multiplier * ATR`` with Wilder ATR (SMA seed) and the usual
    ratcheting.  ATR and band/trend state are recursive and resumed from persisted
    ``indicator_state`` through the shared ``SupertrendCalculator`` (never recomputed from the
    moving candle window).  The first ATR bar starts in a downtrend (TradingView convention).
    Signal: prior direction down, latest direction up.
    Needs ``atr_period + 1`` completed bars (11 by default).
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version=STRATEGY_VERSION,
        side=StrategyRole.COVER,
        category="technical",
        sub_category="trend_following",
        reference_symbols=(),
        required_timeframes=(TIMEFRAME,),
        warmup_bars=11,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec(
                    "atr_period", ParameterKind.INT, 10, 1, 100,
                    description="Wilder ATR length. Default 10.",
                ),
                ParameterSpec(
                    "multiplier", ParameterKind.DECIMAL, Decimal(3), Decimal("0.5"), Decimal(10),
                    description="ATR multiple of the band offset. Default 3.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_INDICATOR),
        methodology=(
            "COVER on the completed bar where Supertrend changes from downtrend to uptrend. "
            "Rule definition only; no alpha or profitability claim."
        ),
        source=(
            "Olivier Seban's Supertrend; TradingView Pine reference ta.supertrend "
            "(hl2 +/- factor * ATR, ratcheting bands, close-through flips)."
        ),
        execution_limitations=(
            "Exit signal only: the engine must evaluate it only while a managed short exists and decides the cover order, quantity and any follow-on action; it never implies a BUY.",
            "The Supertrend line is not used as a stop; the initial direction at the first ATR bar "
            "is a downtrend by convention.",
            "Completed adjacent M1 bars; regular session only; stale data never signals.",
        ),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        period = cls.spec.parameter_schema.validate(params)["atr_period"]
        assert isinstance(period, int)
        return period + 1

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        validated = self.spec.parameter_schema.validate(params)
        period, multiplier = validated["atr_period"], validated["multiplier"]
        assert isinstance(period, int) and isinstance(multiplier, Decimal)
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, period + 1)
        if bars is None:
            return insufficient(self.spec, context)
        adv = advance(
            state, bars, f"{STRATEGY_ID}|{STRATEGY_VERSION}|{period}|{multiplier}",
            fresh=lambda: SupertrendCalculator(period, multiplier),
            restore=SupertrendCalculator.restore,
            feed=lambda calc, bar: calc.update(bar),
        )
        if reason is not None:
            return blocked(self.spec, context, reason, adv.updates)
        if not adjacent(bars[-2], bars[-1]) or adv.prior is None or adv.latest is None:
            return insufficient(self.spec, context, adv.updates)
        _, was_up = parts(adv.prior, 2)
        _, is_up = parts(adv.latest, 2)
        return finish(
            self.spec, context, bars[-1], state, was_up is False and is_up is True, adv.updates
        )
