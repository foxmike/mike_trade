"""Previous completed regular-session low sweep-and-reclaim BUY."""

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

STRATEGY_ID = "previous_day_low_reversal"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1
WARMUP_BARS = 1

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.BUY,
    category="technical",
    sub_category="price_action",
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
        "BUY on a completed regular-session M1 candle "
        "whose low trades at or below the previous fully observed regular-session low "
        "but whose close reclaims strictly above that level."
    ),
    source=(
        "Finetiq Ideas, 'Prior day extreme raid and reclaim' (Trader Vic intraday "
        "return-above-level entry; completed-close SFP variant): "
        "https://ideas.finetiqsystems.com/en/ideas/prior-extreme-raid-reclaim/"
    ),
    execution_limitations=(
        "Entry signal only; no sizing, stops, targets, or order submission.",
        "Each new completed sweep-and-reclaim candle may qualify; same-bar replay is suppressed.",
        "The completed M1 close is a conservative observation of the published intraday "
        "return above the level; no daily-bar confirmation or future candle is used.",
        "Requires a fully observed previous regular exchange session.",
    ),
    reason_codes=REASON_CODES,
)


class PreviousDayLowReversalBuy(BuyStrategy):
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
        found = latest_session_bars(context, 1)
        if reference is None or found is None:
            return insufficient(self.spec, context)
        latest = found[1][-1]
        level = reference.previous.low
        triggered = latest.low <= level < latest.close
        return finish(self.spec, context, latest, state, triggered)
