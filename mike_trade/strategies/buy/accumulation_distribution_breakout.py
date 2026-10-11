"""Long entry when the Accumulation/Distribution line makes a new {period}-bar high."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import BuyStrategy, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.buy._cumulative import SCHEMA, evaluate_breakout, required
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_INDICATOR, STATE_LAST_SIGNAL_BAR

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "accumulation_distribution_breakout"
KIND = "ad"


class AccumulationDistributionBreakoutBuy(BuyStrategy):
    """BUY when the Accumulation/Distribution line rises above its highest value of the previous `period` bars.

    The A/D line adds ((close-low)-(high-close))/(high-low) x volume each bar.  Signal: the line at ``t`` exceeds the maximum of its previous `period` values while
    the prior bar did not exceed its own (first breakout).  The running sum is resumed from persisted
    state; only comparisons with the line's own recent history are used, so the starting level is
    irrelevant.  Needs ``period + 2`` completed bars (22 by default).
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID, version="1.0.0", side=StrategyRole.BUY,
        category="technical", sub_category="volume", reference_symbols=(),
        required_timeframes=(CandleTimeframe.M1,), warmup_bars=22,
        completed_bars_only=True, requires_volume=True,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=CandleTimeframe.M1,
        parameter_schema=SCHEMA,
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR, STATE_INDICATOR),
        methodology="BUY when the Chaikin A/D line exceeds its previous n values (a new n-bar A/D high).",
        source=("Marc Chaikin's Accumulation/Distribution Line; StockCharts ChartSchool, Accumulation/Distribution "
                "Line (ADL = previous ADL + money flow multiplier x volume): "
                "https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/accumulation-distribution-line. Breaking its own n-bar high is the strategy's "
                "precise reading of an A/D breakout, by analogy with OBV breakouts."),
        execution_limitations=("Entry signal only: no stops, sizing, targets, or orders.",
                               "Indicator-only breakout; no price confirmation is added.",
                               "Completed adjacent M1 bars; regular session and BUY eligibility apply."),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        return required(params)

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        return evaluate_breakout(self.spec, KIND, context, params, state)
