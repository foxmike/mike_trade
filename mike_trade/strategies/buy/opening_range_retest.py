"""Opening-range breakout-and-first-retest BUY."""

from __future__ import annotations

from collections.abc import Mapping
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
from mike_trade.strategies.buy._session import current_session_bars
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

STRATEGY_ID = "opening_range_retest"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1
DEFAULT_OPENING_RANGE_MINUTES = 15
WARMUP_BARS = DEFAULT_OPENING_RANGE_MINUTES + 2

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID, version=STRATEGY_VERSION, side=StrategyRole.BUY,
    category="technical", sub_category="breakout", reference_symbols=(),
    required_timeframes=(TIMEFRAME,), warmup_bars=WARMUP_BARS,
    completed_bars_only=True, requires_volume=False,
    trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
    parameter_schema=ParameterSchema((ParameterSpec(
        "opening_range_minutes", ParameterKind.INT,
        default=DEFAULT_OPENING_RANGE_MINUTES, min_value=5, max_value=60,
        description="Opening range length from the 09:30 ET regular-session open.",
    ),)), persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=(
        "Build the high of the first opening_range_minutes completed M1 bars. After the "
        "first completed close above that high, BUY on the first later completed bar "
        "whose low touches or undercuts the level but whose close finishes above it. "
        "A first retest that closes at or below the level invalidates the setup."
    ),
    source=(
        "Ninjacators, The Opening Range Strategy Guide, breakout-pullback method "
        "(wait for breakout and retest of the opening range): https://cdn.ninjacators.com/"
        "wp-content/uploads/20210723201351/The-Opening-Range-Strategy-Guide.pdf"
    ),
    execution_limitations=(
        "Entry signal only; no sizing, stops, targets, or order submission.",
        "Requires an exact, contiguous current regular session from 09:30 ET.",
        "Only the first post-breakout retest can qualify.",
    ), reason_codes=REASON_CODES,
)


class OpeningRangeRetestBuy(BuyStrategy):
    spec = _SPEC

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        value = cls.spec.parameter_schema.validate(params)["opening_range_minutes"]
        assert isinstance(value, int)
        return value + 2

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        length = self.spec.parameter_schema.validate(params)["opening_range_minutes"]
        assert isinstance(length, int)
        reason = gate(self.spec, context, state)
        if reason is not None:
            return blocked(self.spec, context, reason)
        found = current_session_bars(context)
        if found is None or len(found[1]) < length + 2:
            return insufficient(self.spec, context)
        bars = found[1]
        latest = bars[-1]
        level = max(bar.high for bar in bars[:length])
        after_range = bars[length:]
        breakout_index = next(
            (index for index, bar in enumerate(after_range[:-1]) if bar.close > level), None
        )
        if breakout_index is None:
            return finish(self.spec, context, latest, state, False)
        between = after_range[breakout_index + 1:-1]
        first_retest_is_latest = not any(bar.low <= level for bar in between)
        triggered = first_retest_is_latest and latest.low <= level < latest.close
        return finish(self.spec, context, latest, state, triggered)
