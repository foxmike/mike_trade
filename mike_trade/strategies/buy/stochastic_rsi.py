"""Long entry when Stochastic RSI climbs back above its oversold level."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import BuyStrategy, ParameterKind, ParameterSchema, ParameterSpec, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_INDICATOR, STATE_LAST_SIGNAL_BAR, adjacent, advance, blocked, closed_bars, crossed_above, finish, gate, insufficient, known
from mike_trade.strategies.indicators.oscillators import StochRSICalculator

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "stochastic_rsi"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


class StochasticRsiBuy(BuyStrategy):
    """BUY when StochRSI crosses from at-or-below to above `oversold` (default 0.20).

    ``StochRSI = (RSI - lowest RSI) / (highest RSI - lowest RSI)`` over the last `stoch_period`
    Wilder RSI(`rsi_period`) values.  Differs from ``stochastic_oversold_reversal`` (the same
    calculation on price) and ``rsi_oversold_reversal`` (raw RSI).  A flat RSI window is
    undefined.  The Wilder RSI and its window are resumed from persisted state.  Needs
    ``rsi_period + stoch_period + 1`` completed bars (29 by default).
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID, version=STRATEGY_VERSION, side=StrategyRole.BUY,
        category="technical", sub_category="oscillator", reference_symbols=(),
        required_timeframes=(TIMEFRAME,), warmup_bars=29,
        completed_bars_only=True, requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema((
            ParameterSpec("rsi_period", ParameterKind.INT, 14, 2, 100,
                          description="Wilder RSI length. Default 14."),
            ParameterSpec("stoch_period", ParameterKind.INT, 14, 2, 100,
                          description="RSI range look-back. Default 14."),
            ParameterSpec("oversold", ParameterKind.DECIMAL, Decimal("0.2"), Decimal("0.01"), Decimal("0.5"),
                          description="Oversold level on the 0-1 scale. Default 0.20."),
        )),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_INDICATOR),
        methodology="BUY when raw Stochastic RSI crosses from at-or-below to above the oversold level on a completed bar.",
        source=("Tushar Chande and Stanley Kroll, The New Technical Trader (1994), StochRSI; "
                "StockCharts ChartSchool, StochRSI; Fidelity Learning Center, StochRSI: buy when it "
                "rises from below 0.20 back above it."),
        execution_limitations=("Raw (unsmoothed) StochRSI; the %K/%D smoothed variant is not used.",
                               "Completed adjacent M1 bars; regular session and BUY eligibility apply."),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        v = cls.spec.parameter_schema.validate(params)
        return v["rsi_period"] + v["stoch_period"] + 1  # type: ignore[operator]

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        v = self.spec.parameter_schema.validate(params)
        rsi_period, stoch_period, oversold = v["rsi_period"], v["stoch_period"], v["oversold"]
        assert isinstance(rsi_period, int) and isinstance(stoch_period, int)
        assert isinstance(oversold, Decimal)
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, rsi_period + stoch_period + 1)
        if bars is None:
            return insufficient(self.spec, context)
        adv = advance(state, bars, f"{STRATEGY_ID}|{STRATEGY_VERSION}|{rsi_period}|{stoch_period}",
                      fresh=lambda: StochRSICalculator(rsi_period, stoch_period),
                      restore=StochRSICalculator.restore, feed=lambda calc, bar: calc.update(bar.close))
        if reason is not None:
            return blocked(self.spec, context, reason, adv.updates)
        if not adjacent(bars[-2], bars[-1]) or not known(adv.prior, adv.latest):
            return insufficient(self.spec, context, adv.updates)
        return finish(self.spec, context, bars[-1], state,
                      crossed_above(adv.prior, oversold, adv.latest, oversold), adv.updates)
