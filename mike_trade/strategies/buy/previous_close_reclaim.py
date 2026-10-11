"""Previous regular-session close reclaim BUY."""

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

STRATEGY_ID = "previous_close_reclaim"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1
BASELINE_MINUTES = 30
ENTRY_END = time(11, 0)
WARMUP_BARS = BASELINE_MINUTES + 1

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.BUY,
    category="technical",
    sub_category="intraday_session",
    reference_symbols=(),
    required_timeframes=(TIMEFRAME,),
    warmup_bars=WARMUP_BARS,
    completed_bars_only=True,
    requires_volume=True,
    trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
    trigger_timeframe=TIMEFRAME,
    parameter_schema=ParameterSchema(
        (
            ParameterSpec(
                "min_gap_percent",
                ParameterKind.DECIMAL,
                default=Decimal("0.5"),
                min_value=Decimal("0"),
                max_value=Decimal("100"),
                description="Minimum regular-session gap-down percentage.",
            ),
            ParameterSpec(
                "volume_multiplier",
                ParameterKind.DECIMAL,
                default=Decimal("1.5"),
                min_value=Decimal("1"),
                max_value=Decimal("10"),
                description="Trigger volume divided by first-30-minute average volume.",
            ),
        )
    ),
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=(
        "Red-to-green reclaim: after a gap down of at least min_gap_percent, BUY from "
        "after 10:00 through 11:00 ET when a completed M1 close crosses above the previous "
        "regular-session close, also closes above session VWAP, and its volume is at "
        "least volume_multiplier times the first 30 completed minutes' average."
    ),
    source=(
        "FinWiz, 'Red to Green Move: How to Trade the Intraday Reversal' (gap down "
        "at least 0.5%, completed one-minute close above prior close, VWAP, expanding "
        "volume, best in first 60-90 minutes): https://finwiz.io/day-trading/"
        "red-to-green-move"
    ),
    execution_limitations=(
        "Entry signal only; no sizing, stops, targets, or order submission.",
        "Requires the actual previous completed exchange session; no calendar-day proxy.",
        "Requires the complete first 30 regular-session minutes and shared session VWAP.",
    ),
    reason_codes=REASON_CODES,
)


class PreviousCloseReclaimBuy(BuyStrategy):
    spec = _SPEC

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        values = self.spec.parameter_schema.validate(params)
        minimum = values["min_gap_percent"]
        multiplier = values["volume_multiplier"]
        assert isinstance(minimum, Decimal) and isinstance(multiplier, Decimal)
        reason = gate(self.spec, context, state)
        if reason is not None:
            return blocked(self.spec, context, reason)
        reference = complete_session_reference(context)
        found = current_session_bars(context)
        vwap = context.indicators.get("vwap")
        if (
            reference is None
            or found is None
            or len(found[1]) < WARMUP_BARS
            or not isinstance(vwap, Decimal)
            or not vwap.is_finite()
            or vwap <= 0
        ):
            return insufficient(self.spec, context)
        bars = found[1]
        prior, latest = bars[-2:]
        wall_time = latest.end.astimezone(NEW_YORK).time()
        level = reference.previous.close
        gap_percent = (level - reference.current_open) / level * 100
        baseline_volume = sum(
            (bar.volume for bar in bars[:BASELINE_MINUTES]), Decimal(0)
        ) / BASELINE_MINUTES
        if baseline_volume <= 0:
            return insufficient(self.spec, context)
        triggered = (
            time(10) <= wall_time <= ENTRY_END
            and gap_percent >= minimum
            and reference.current_open < level
            and prior.close <= level < latest.close
            and latest.close > vwap
            and latest.volume >= baseline_volume * multiplier
        )
        return finish(self.spec, context, latest, state, triggered)
