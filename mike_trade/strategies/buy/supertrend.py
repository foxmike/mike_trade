"""Long entry when the Supertrend flips from downtrend to uptrend."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import BuyStrategy, ParameterKind, ParameterSchema, ParameterSpec, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_INDICATOR, STATE_LAST_SIGNAL_BAR, adjacent, advance, blocked, closed_bars, finish, gate, insufficient, parts
from mike_trade.strategies.indicators.extended import SupertrendCalculator

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "supertrend"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


class SupertrendBuy(BuyStrategy):
    """BUY when Supertrend turns from down to up: the close finishes above the final upper band.

    Bands are ``(high + low) / 2 +/- multiplier * ATR`` (Wilder ATR, SMA seed) with the usual
    ratcheting: the upper band only falls and the lower band only rises unless price closed
    through it.  Both the ATR and the band/trend state are recursive and resumed from persisted
    state.  Needs ``atr_period + 1`` completed bars (11 by default).  The first ATR bar starts
    in a downtrend (TradingView ``ta.supertrend`` convention), an interpretation recorded in
    the calculator.
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID, version=STRATEGY_VERSION, side=StrategyRole.BUY,
        category="technical", sub_category="trend_following", reference_symbols=(),
        required_timeframes=(TIMEFRAME,), warmup_bars=11,
        completed_bars_only=True, requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema((
            ParameterSpec("atr_period", ParameterKind.INT, 10, 1, 100,
                          description="Wilder ATR length. Default 10."),
            ParameterSpec("multiplier", ParameterKind.DECIMAL, Decimal(3), Decimal("0.5"), Decimal(10),
                          description="ATR multiple of the band offset. Default 3."),
        )),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_INDICATOR),
        methodology=("BUY on the completed bar where the Supertrend direction changes from "
                     "downtrend to uptrend. Rule definition only; no profitability claim."),
        source=("Olivier Seban's Supertrend; TradingView Pine reference ta.supertrend "
                "(hl2 +/- factor * ATR, ratcheting bands, close-through flips)."),
        execution_limitations=("Entry signal only: no stops, sizing, targets, or orders; the "
                               "Supertrend line is not used as a stop.",
                               "Initial direction at the first ATR bar is a downtrend (convention).",
                               "Completed adjacent M1 bars; regular session and BUY eligibility apply."),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        period = cls.spec.parameter_schema.validate(params)["atr_period"]
        assert isinstance(period, int)
        return period + 1

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        validated = self.spec.parameter_schema.validate(params)
        period, multiplier = validated["atr_period"], validated["multiplier"]
        assert isinstance(period, int) and isinstance(multiplier, Decimal)
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, period + 1)
        if bars is None:
            return insufficient(self.spec, context)
        adv = advance(state, bars, f"{STRATEGY_ID}|{STRATEGY_VERSION}|{period}|{multiplier}",
                      fresh=lambda: SupertrendCalculator(period, multiplier),
                      restore=SupertrendCalculator.restore,
                      feed=lambda calc, bar: calc.update(bar))
        if reason is not None:
            return blocked(self.spec, context, reason, adv.updates)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context, adv.updates)
        if adv.prior is None or adv.latest is None:
            return insufficient(self.spec, context, adv.updates)
        _, was_up = parts(adv.prior, 2)
        _, is_up = parts(adv.latest, 2)
        return finish(self.spec, context, bars[-1], state, was_up is False and is_up is True,
                      adv.updates)
