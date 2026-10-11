"""Long entry when the True Strength Index crosses above its zero line."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import BuyStrategy, ParameterKind, ParameterSchema, ParameterSpec, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_INDICATOR, STATE_LAST_SIGNAL_BAR, adjacent, advance, blocked, closed_bars, crossed_above, finish, gate, insufficient, known
from mike_trade.strategies.indicators.oscillators import TSICalculator

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "tsi_momentum"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


class TsiMomentumBuy(BuyStrategy):
    """BUY when TSI(25,13) crosses from at-or-below to above zero.

    ``TSI = 100 * EMA(EMA(change, long), short) / EMA(EMA(|change|, long), short)`` where
    ``change`` is the close-to-close difference.  Positive TSI means smoothed momentum is up.
    All four EMAs are SMA-seeded and resumed from persisted state.  Needs ``long + short + 1``
    completed bars (39 by default).
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID, version=STRATEGY_VERSION, side=StrategyRole.BUY,
        category="technical", sub_category="momentum", reference_symbols=(),
        required_timeframes=(TIMEFRAME,), warmup_bars=39,
        completed_bars_only=True, requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema((
            ParameterSpec("long_period", ParameterKind.INT, 25, 2, 200,
                          description="First (long) EMA smoothing of price change. Default 25."),
            ParameterSpec("short_period", ParameterKind.INT, 13, 2, 100,
                          description="Second (short) EMA smoothing. Default 13."),
        )),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_INDICATOR),
        methodology="BUY when the True Strength Index crosses from at-or-below to above zero on a completed bar.",
        source=("William Blau, Momentum, Direction, and Divergence (1995), True Strength Index; "
                "StockCharts ChartSchool, True Strength Index (TSI): centerline crossovers."),
        execution_limitations=("The signal-line crossover variant is not used.",
                               "SMA-seeded EMAs; entry signal only.",
                               "Completed adjacent M1 bars; regular session and BUY eligibility apply."),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        v = cls.spec.parameter_schema.validate(params)
        return v["long_period"] + v["short_period"] + 1  # type: ignore[operator]

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        v = self.spec.parameter_schema.validate(params)
        long_period, short_period = v["long_period"], v["short_period"]
        assert isinstance(long_period, int) and isinstance(short_period, int)
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, long_period + short_period + 1)
        if bars is None:
            return insufficient(self.spec, context)
        adv = advance(state, bars, f"{STRATEGY_ID}|{STRATEGY_VERSION}|{long_period}|{short_period}",
                      fresh=lambda: TSICalculator(long_period, short_period),
                      restore=TSICalculator.restore, feed=lambda calc, bar: calc.update(bar.close))
        if reason is not None:
            return blocked(self.spec, context, reason, adv.updates)
        if not adjacent(bars[-2], bars[-1]) or not known(adv.prior, adv.latest):
            return insufficient(self.spec, context, adv.updates)
        zero = Decimal(0)
        return finish(self.spec, context, bars[-1], state,
                      crossed_above(adv.prior, zero, adv.latest, zero), adv.updates)
