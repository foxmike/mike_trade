"""Gap-down fade BUY confirmed by a completed reclaim of the session open."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import (
    BuyStrategy,
    ParameterKind,
    ParameterSchema,
    ParameterSpec,
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

STRATEGY_ID = "gap_fill"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1
WARMUP_BARS = 2

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.BUY,
    category="technical",
    sub_category="gap_trading",
    reference_symbols=(),
    required_timeframes=(TIMEFRAME,),
    warmup_bars=WARMUP_BARS,
    completed_bars_only=True,
    requires_volume=False,
    trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
    trigger_timeframe=TIMEFRAME,
    parameter_schema=ParameterSchema(
        (
            ParameterSpec(
                "min_gap_percent",
                ParameterKind.DECIMAL,
                default=Decimal("0"),
                min_value=Decimal("0"),
                max_value=Decimal("100"),
                description="Minimum strict regular-session gap-down percentage.",
            ),
        )
    ),
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=(
        "For a regular-session gap down of at least min_gap_percent, BUY when a "
        "completed M1 close crosses back above the current regular-session open. "
        "The previous completed regular-session close is the unfilled-gap reference."
    ),
    source=(
        "Stock Market Watch, Gap Fill Trading Strategy (fade a gap after reversal "
        "confirmation, using loss/reclaim of the opening price as confirmation): "
        "https://stockmarketwatch.com/edu/trading-strategies/day-trading/"
        "gap-fill-trading"
    ),
    execution_limitations=(
        "Entry signal only; the previous close is a reference/target, not an exit order.",
        "Requires a fully observed previous regular session and exact current-session open.",
        "Regular-session completed bars only; premarket prices are excluded.",
    ),
    reason_codes=REASON_CODES,
)


class GapFillBuy(BuyStrategy):
    spec = _SPEC

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        minimum = self.spec.parameter_schema.validate(params)["min_gap_percent"]
        assert isinstance(minimum, Decimal)
        reason = gate(self.spec, context, state)
        if reason is not None:
            return blocked(self.spec, context, reason)
        reference = complete_session_reference(context)
        found = latest_session_bars(context, 2)
        if reference is None or found is None or len(found[1]) < 2:
            return insufficient(self.spec, context)
        bars = found[1]
        prior, latest = bars[-2:]
        previous_close = reference.previous.close
        gap_percent = (previous_close - reference.current_open) / previous_close * 100
        triggered = (
            gap_percent > 0
            and gap_percent >= minimum
            and prior.close <= reference.current_open < latest.close
        )
        return finish(self.spec, context, latest, state, triggered)
