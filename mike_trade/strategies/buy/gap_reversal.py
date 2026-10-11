"""Gap-down opening-range and VWAP recovery BUY."""

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

STRATEGY_ID = "gap_reversal"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1
DEFAULT_OPENING_RANGE_MINUTES = 15
WARMUP_BARS = DEFAULT_OPENING_RANGE_MINUTES + 1

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
    requires_volume=True,
    trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
    trigger_timeframe=TIMEFRAME,
    parameter_schema=ParameterSchema(
        (
            ParameterSpec(
                "opening_range_minutes",
                ParameterKind.INT,
                default=DEFAULT_OPENING_RANGE_MINUTES,
                min_value=5,
                max_value=30,
                description="Opening range length from the 09:30 ET regular-session open.",
            ),
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
        "For a regular-session gap down of at least min_gap_percent, form the first "
        "opening_range_minutes of completed M1 bars. BUY when a later completed close "
        "crosses above the opening-range high and also closes above session VWAP."
    ),
    source=(
        "BreakPoint, Gap Down Trading Strategy, 'gap-down recovery' setup (wait for "
        "the opening range; long after price closes above its high and VWAP): "
        "https://brkpoint.in/tradeguide/learn/gap-down-trading-strategy/"
    ),
    execution_limitations=(
        "Entry signal only; no sizing, stops, targets, or order submission.",
        "Requires fully observed prior-session OHLC, contiguous current-session M1 bars, "
        "and the shared completed-bar session VWAP.",
        "Premarket and after-hours bars are excluded.",
    ),
    reason_codes=REASON_CODES,
)


class GapReversalBuy(BuyStrategy):
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
        reference = complete_session_reference(context)
        found = current_session_bars(context)
        vwap = context.indicators.get("vwap")
        if (
            reference is None
            or found is None
            or len(found[1]) < length + 1
            or not isinstance(vwap, Decimal)
            or not vwap.is_finite()
            or vwap <= 0
        ):
            return insufficient(self.spec, context)
        bars = found[1]
        prior, latest = bars[-2:]
        previous_close = reference.previous.close
        gap_percent = (previous_close - reference.current_open) / previous_close * 100
        opening_high = max(bar.high for bar in bars[:length])
        triggered = (
            gap_percent > 0
            and gap_percent >= minimum
            and prior.close <= opening_high < latest.close
            and latest.close > vwap
        )
        return finish(self.spec, context, latest, state, triggered)
