"""Long entry on an ATR volatility-expansion thrust."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import BuyStrategy, ParameterKind, ParameterSchema, ParameterSpec, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_INDICATOR, STATE_LAST_SIGNAL_BAR, adjacent, advance, blocked, closed_bars, finish, gate, insufficient
from mike_trade.strategies.indicators.extended import SnapshotATRCalculator

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "atr_expansion_breakout"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


class AtrExpansionBreakoutBuy(BuyStrategy):
    """BUY when the close rises more than `multiplier` x the prior bar's ATR above the prior close.

    ``close[t] - close[t-1] > multiplier * ATR[t-1]`` with Wilder ATR (SMA seed) measured on the
    bar *before* the thrust, so the expansion bar does not inflate its own threshold.  The ATR is
    recursive and resumed from persisted state.  Needs ``period + 1`` completed bars (15 by
    default).  An M1 closed-bar adaptation of volatility-breakout systems (entry when price
    moves a multiple of recent volatility beyond a reference price).
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID, version=STRATEGY_VERSION, side=StrategyRole.BUY,
        category="technical", sub_category="volatility", reference_symbols=(),
        required_timeframes=(TIMEFRAME,), warmup_bars=15,
        completed_bars_only=True, requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema((
            ParameterSpec("period", ParameterKind.INT, 14, 2, 100,
                          description="Wilder ATR length. Default 14."),
            ParameterSpec("multiplier", ParameterKind.DECIMAL, Decimal(2), Decimal("0.5"), Decimal(10),
                          description="Required close-to-close thrust in prior-bar ATRs. Default 2."),
        )),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_INDICATOR),
        methodology=("BUY when close minus the prior close exceeds multiplier times the Wilder "
                     "ATR of the prior bar (a volatility-expansion thrust)."),
        source=("J. Welles Wilder, New Concepts in Technical Trading Systems (1978), Average True "
                "Range and Volatility System; Perry Kaufman, Trading Systems and Methods, "
                "volatility breakout systems; StockCharts ChartSchool, Average True Range (ATR). "
                "Multiplier-of-ATR thrust is the shared rule; the M1 closed-bar form is an adaptation."),
        execution_limitations=("Entry signal only: no stops, sizing, targets, or orders.",
                               "Reference price is the prior close, not the session open used by "
                               "daily-bar variants.",
                               "Completed adjacent M1 bars; regular session and BUY eligibility apply."),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        period = cls.spec.parameter_schema.validate(params)["period"]
        assert isinstance(period, int)
        return period + 1

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        validated = self.spec.parameter_schema.validate(params)
        period, multiplier = validated["period"], validated["multiplier"]
        assert isinstance(period, int) and isinstance(multiplier, Decimal)
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, period + 1)
        if bars is None:
            return insufficient(self.spec, context)
        adv = advance(state, bars, f"{STRATEGY_ID}|{STRATEGY_VERSION}|{period}",
                      fresh=lambda: SnapshotATRCalculator(period),
                      restore=SnapshotATRCalculator.restore,
                      feed=lambda calc, bar: calc.update(bar))
        if reason is not None:
            return blocked(self.spec, context, reason, adv.updates)
        if not adjacent(bars[-2], bars[-1]) or adv.prior is None:
            return insufficient(self.spec, context, adv.updates)
        triggered = bars[-1].close - bars[-2].close > multiplier * adv.prior
        return finish(self.spec, context, bars[-1], state, triggered, adv.updates)
