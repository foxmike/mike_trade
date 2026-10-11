"""Long entry when the whole short-term EMA ribbon rises above the whole long-term ribbon."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import BuyStrategy, ParameterSchema, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_INDICATOR, STATE_LAST_SIGNAL_BAR, adjacent, advance, blocked, closed_bars, crossed_above, finish, gate, insufficient, known, parts
from mike_trade.strategies.indicators.extended import EMARibbonCalculator

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "moving_average_ribbon"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1
SHORT_PERIODS = (3, 5, 8, 10, 12, 15)
LONG_PERIODS = (30, 35, 40, 45, 50, 60)
BARS_NEEDED = max(LONG_PERIODS) + 1


class MovingAverageRibbonBuy(BuyStrategy):
    """BUY when the lowest short-group EMA crosses above the highest long-group EMA.

    Daryl Guppy's Multiple Moving Average ribbon: short group EMAs 3/5/8/10/12/15 and long group
    30/35/40/45/50/60.  Signal: ``min(short EMAs)[t-1] <= max(long EMAs)[t-1]`` and
    ``min(short EMAs)[t] > max(long EMAs)[t]``, i.e. the entire short ribbon has just moved above
    the entire long ribbon.  EMAs are SMA-seeded and resumed from persisted state.  Needs 61
    completed bars.
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID, version=STRATEGY_VERSION, side=StrategyRole.BUY,
        category="technical", sub_category="moving_average", reference_symbols=(),
        required_timeframes=(TIMEFRAME,), warmup_bars=BARS_NEEDED,
        completed_bars_only=True, requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema(()),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_INDICATOR),
        methodology=("BUY when every short-term EMA of the Guppy ribbon has risen above every "
                     "long-term EMA (group crossover) on a completed bar."),
        source=("Daryl Guppy, Trading Tactics / The Guppy Multiple Moving Average (GMMA): "
                "six short EMAs (3-15) versus six long EMAs (30-60); group separation marks a "
                "trend change."),
        execution_limitations=("Fixed Guppy periods applied to M1 bars; entry signal only.",
                               "Completed adjacent M1 bars; regular session and BUY eligibility apply."),
        reason_codes=REASON_CODES,
    )

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        self.spec.parameter_schema.validate(params)
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, BARS_NEEDED)
        if bars is None:
            return insufficient(self.spec, context)
        adv = advance(state, bars, f"{STRATEGY_ID}|{STRATEGY_VERSION}",
                      fresh=lambda: EMARibbonCalculator(SHORT_PERIODS, LONG_PERIODS),
                      restore=EMARibbonCalculator.restore,
                      feed=lambda calc, bar: calc.update(bar.close))
        if reason is not None:
            return blocked(self.spec, context, reason, adv.updates)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context, adv.updates)
        short_prior, long_prior = parts(adv.prior, 2)
        short_latest, long_latest = parts(adv.latest, 2)
        if not known(short_prior, long_prior, short_latest, long_latest):
            return insufficient(self.spec, context, adv.updates)
        return finish(self.spec, context, bars[-1], state,
                      crossed_above(short_prior, long_prior, short_latest, long_latest),
                      adv.updates)
