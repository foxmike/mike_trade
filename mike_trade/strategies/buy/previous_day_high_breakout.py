"""Previous completed regular-session high breakout BUY."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import (
    BuyStrategy,
    ParameterSchema,
    StrategyEvaluation,
    StrategySpec,
    TriggerKind,
)
from mike_trade.strategies.buy._session import (
    complete_session_reference,
    latest_session_bars,
)
from mike_trade.strategies.closed_bar import (
    REASON_CODES,
    STATE_LAST_SIGNAL_BAR,
    blocked,
    finish,
    gate,
    insufficient,
)

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "previous_day_high_breakout"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1
WARMUP_BARS = 2

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.BUY,
    category="technical",
    sub_category="breakout",
    reference_symbols=(),
    required_timeframes=(TIMEFRAME,),
    warmup_bars=WARMUP_BARS,
    completed_bars_only=True,
    requires_volume=False,
    trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
    trigger_timeframe=TIMEFRAME,
    parameter_schema=ParameterSchema(()),
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=(
        "BUY when the latest completed regular-session M1 close crosses strictly above "
        "the high of the previous fully observed regular exchange session."
    ),
    source=(
        "TradeIQ, 'Martin Luke: High-Momentum Swing Trading', intraday PDH entry "
        "tactic (enter when a stock breaks above the previous day's high): "
        "https://www.tradeiqmarket.com/strategy/martin-luke-swing-trading"
    ),
    execution_limitations=(
        "Entry signal only; no sizing, stops, targets, or order submission.",
        "Uses a completed M1 close as conservative confirmation of the published "
        "intraday break trigger, not an intrabar stop order.",
        "Requires a fully observed previous regular exchange session.",
    ),
    reason_codes=REASON_CODES,
)


class PreviousDayHighBreakoutBuy(BuyStrategy):
    spec = _SPEC

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        self.spec.parameter_schema.validate(params)
        reason = gate(self.spec, context, state)
        if reason is not None:
            return blocked(self.spec, context, reason)
        reference = complete_session_reference(context)
        found = latest_session_bars(context, 2)
        if reference is None or found is None or len(found[1]) < 2:
            return insufficient(self.spec, context)
        prior, latest = found[1][-2:]
        level = reference.previous.high
        return finish(
            self.spec, context, latest, state, prior.close <= level < latest.close
        )
