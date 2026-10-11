"""Long exit when Supertrend flips from uptrend to downtrend."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import (
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
from mike_trade.strategies.sell.base import SellRule

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "supertrend_bearish_flip"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


class SupertrendBearishFlipSell(SellRule):
    """SELL when the recursive Supertrend direction changes from up to down."""

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version=STRATEGY_VERSION,
        side=StrategyRole.SELL,
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
                    "atr_period",
                    ParameterKind.INT,
                    10,
                    1,
                    100,
                    description="Wilder ATR length. Default 10.",
                ),
                ParameterSpec(
                    "multiplier",
                    ParameterKind.DECIMAL,
                    Decimal(3),
                    Decimal("0.5"),
                    Decimal(10),
                    description="ATR multiple of the band offset. Default 3.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_INDICATOR),
        methodology=(
            "SELL on the completed bar where Supertrend changes from uptrend to "
            "downtrend after close finishes below the final lower band."
        ),
        source=(
            "Olivier Seban's Supertrend; TradingView Pine reference ta.supertrend "
            "(hl2 +/- factor * ATR, ratcheting bands, close-through flips)."
        ),
        execution_limitations=(
            "Advisory long exit only; the Supertrend line is not installed "
            "as a broker protective stop.",
            "Initial direction at the first ATR bar is downtrend by the TradingView convention.",
            "Recursive state is snapshot-restored; crossing bars must be "
            "adjacent completed regular-session M1 bars.",
        ),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        period = cls.spec.parameter_schema.validate(params)["atr_period"]
        assert isinstance(period, int)
        return period + 1

    def evaluate_trigger(
        self, context: MarketContext, params: Mapping[str, object], state: Mapping[str, object]
    ) -> StrategyEvaluation:
        values = self.spec.parameter_schema.validate(params)
        period, multiplier = values["atr_period"], values["multiplier"]
        assert isinstance(period, int) and isinstance(multiplier, Decimal)
        bars = closed_bars(self.spec, context, period + 1)
        if bars is None:
            return insufficient(self.spec, context)
        if any(not adjacent(a, b) for a, b in zip(bars, bars[1:], strict=False)):
            return insufficient(self.spec, context)
        reason = gate(self.spec, context, state)
        adv = advance(
            state,
            bars,
            f"{STRATEGY_ID}|{STRATEGY_VERSION}|{period}|{multiplier}",
            fresh=lambda: SupertrendCalculator(period, multiplier),
            restore=SupertrendCalculator.restore,
            feed=lambda calc, bar: calc.update(bar),
        )
        if reason is not None:
            return blocked(self.spec, context, reason, adv.updates)
        if adv.prior is None or adv.latest is None:
            return insufficient(self.spec, context, adv.updates)
        _, was_up = parts(adv.prior, 2)
        _, is_up = parts(adv.latest, 2)
        return finish(
            self.spec, context, bars[-1], state, was_up is True and is_up is False, adv.updates
        )
