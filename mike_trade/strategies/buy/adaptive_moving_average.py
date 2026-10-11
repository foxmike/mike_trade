"""Long entry when the close crosses above Kaufman's Adaptive Moving Average."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import BuyStrategy, ParameterKind, ParameterSchema, ParameterSpec, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_INDICATOR, STATE_LAST_SIGNAL_BAR, adjacent, advance, blocked, closed_bars, crossed_above, finish, gate, insufficient, require_param
from mike_trade.strategies.indicators.extended import KAMACalculator

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "adaptive_moving_average"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


class AdaptiveMovingAverageBuy(BuyStrategy):
    """BUY when the close crosses from at-or-below to above KAMA.

    ``ER = |close - close[n]| / sum|close[i] - close[i-1]|``,
    ``SC = (ER * (2/(fast+1) - 2/(slow+1)) + 2/(slow+1))^2``, ``KAMA = KAMA[-1] + SC * (close -
    KAMA[-1])`` seeded with the SMA of the first `er_period` closes.  KAMA is recursive and
    resumed from persisted state.  Needs ``er_period + 1`` completed bars (11 by default).
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID, version=STRATEGY_VERSION, side=StrategyRole.BUY,
        category="technical", sub_category="moving_average", reference_symbols=(),
        required_timeframes=(TIMEFRAME,), warmup_bars=11,
        completed_bars_only=True, requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema((
            ParameterSpec("er_period", ParameterKind.INT, 10, 2, 100,
                          description="Efficiency-ratio look-back. Default 10."),
            ParameterSpec("fast_period", ParameterKind.INT, 2, 1, 50,
                          description="Fastest EMA equivalent. Default 2."),
            ParameterSpec("slow_period", ParameterKind.INT, 30, 2, 200,
                          description="Slowest EMA equivalent; must exceed fast_period. Default 30."),
        )),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_INDICATOR),
        methodology="BUY when the close crosses above Kaufman's Adaptive Moving Average on a completed bar.",
        source=("Perry Kaufman, Smarter Trading (1995) / Trading Systems and Methods; StockCharts "
                "ChartSchool, Kaufman's Adaptive Moving Average (KAMA), 10/2/30 defaults."),
        execution_limitations=("KAMA seed is the SMA of the first er_period closes (StockCharts "
                               "convention); later values do not depend on the history window.",
                               "Completed adjacent M1 bars; regular session and BUY eligibility apply."),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        er = cls.spec.parameter_schema.validate(params)["er_period"]
        assert isinstance(er, int)
        return er + 1

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        v = self.spec.parameter_schema.validate(params)
        er, fast, slow = v["er_period"], v["fast_period"], v["slow_period"]
        assert isinstance(er, int) and isinstance(fast, int) and isinstance(slow, int)
        require_param(fast < slow, "fast_period < slow_period", "fast_period")
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, er + 1)
        if bars is None:
            return insufficient(self.spec, context)
        adv = advance(state, bars, f"{STRATEGY_ID}|{STRATEGY_VERSION}|{er}|{fast}|{slow}",
                      fresh=lambda: KAMACalculator(er, fast, slow),
                      restore=KAMACalculator.restore,
                      feed=lambda calc, bar: calc.update(bar.close))
        if reason is not None:
            return blocked(self.spec, context, reason, adv.updates)
        if not adjacent(bars[-2], bars[-1]) or adv.prior is None or adv.latest is None:
            return insufficient(self.spec, context, adv.updates)
        return finish(self.spec, context, bars[-1], state,
                      crossed_above(bars[-2].close, adv.prior, bars[-1].close, adv.latest),
                      adv.updates)
