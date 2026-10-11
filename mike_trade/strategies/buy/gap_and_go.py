"""Gap-up opening-range breakout BUY (the Gap-and-Go setup)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import time
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.scheduling.calendar import NEW_YORK
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
    current_session_bars,
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

STRATEGY_ID = "gap_and_go"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1
DEFAULT_OPENING_RANGE_MINUTES = 1
DEFAULT_MIN_GAP_PERCENT = Decimal("4")
WARMUP_BARS = DEFAULT_OPENING_RANGE_MINUTES + 1
ENTRY_END = time(10, 0)

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
                "opening_range_minutes",
                ParameterKind.INT,
                default=DEFAULT_OPENING_RANGE_MINUTES,
                min_value=1,
                max_value=15,
                description="Opening range length from the 09:30 ET regular-session open.",
            ),
            ParameterSpec(
                "min_gap_percent",
                ParameterKind.DECIMAL,
                default=DEFAULT_MIN_GAP_PERCENT,
                min_value=Decimal("0"),
                max_value=Decimal("100"),
                description="Minimum strict regular-session gap-up percentage.",
            ),
        )
    ),
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=(
        "When the current regular session opens strictly above the previous completed "
        "regular-session close by at least min_gap_percent, BUY on the first completed "
        "M1 close by 10:00 ET that crosses above the high of the configured opening range."
    ),
    source=(
        "Warrior Trading, Day Trading Course Class 12, Gap and Go setups (09:30-10:00 "
        "and one-minute opening-range breakout): https://media.warriortrading.com/"
        "2014/09/WarriorTrading-DayTradingCourse-Class12.pdf"
    ),
    execution_limitations=(
        "Entry signal only; no sizing, stops, targets, or order submission.",
        "Requires a fully observed previous regular session and a contiguous current "
        "regular session from the exchange open.",
        "Premarket prices do not define the gap or opening range.",
        "Signals are limited to completed bars ending no later than 10:00 ET.",
    ),
    reason_codes=REASON_CODES,
)


class GapAndGoBuy(BuyStrategy):
    spec = _SPEC

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        length = cls.spec.parameter_schema.validate(params)["opening_range_minutes"]
        assert isinstance(length, int)
        return length + 1

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        values = self.spec.parameter_schema.validate(params)
        length = values["opening_range_minutes"]
        minimum = values["min_gap_percent"]
        assert isinstance(length, int) and isinstance(minimum, Decimal)
        reason = gate(self.spec, context, state)
        if reason is not None:
            return blocked(self.spec, context, reason)
        supplied = context.candles.get(TIMEFRAME, ())
        if not supplied:
            return insufficient(self.spec, context)
        if supplied[-1].end.astimezone(NEW_YORK).time() > ENTRY_END:
            return finish(self.spec, context, supplied[-1], state, False)
        reference = complete_session_reference(context)
        found = current_session_bars(context)
        if reference is None or found is None or len(found[1]) < length + 1:
            return insufficient(self.spec, context)
        bars = found[1]
        latest = bars[-1]
        previous_close = reference.previous.close
        gap_percent = (reference.current_open - previous_close) / previous_close * 100
        if gap_percent <= 0 or gap_percent < minimum:
            return finish(self.spec, context, latest, state, False)
        opening_high = max(bar.high for bar in bars[:length])
        prior = bars[-2]
        earlier_breakout = any(bar.close > opening_high for bar in bars[length:-1])
        triggered = (
            not earlier_breakout and prior.close <= opening_high < latest.close
        )
        return finish(self.spec, context, latest, state, triggered)
