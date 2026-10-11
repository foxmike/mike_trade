"""Exit a long on a completed close below the regular-session opening range."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import (
    ParameterKind,
    ParameterSchema,
    ParameterSpec,
    StrategyEvaluation,
    StrategySpec,
    TriggerKind,
)
from mike_trade.strategies.closed_bar import (
    REASON_CODES,
    STATE_LAST_SIGNAL_BAR,
    adjacent,
    blocked,
    closed_bars,
    finish,
    gate,
    insufficient,
)
from mike_trade.strategies.sell.base import SellRule

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

_NEW_YORK = ZoneInfo("America/New_York")
_SESSION_OPEN = time(9, 30)


class OpeningRangeBreakdownSell(SellRule):
    spec = StrategySpec(
        strategy_id="opening_range_breakdown",
        version="1.0.0",
        side=StrategyRole.SELL,
        category="technical",
        sub_category="intraday_session",
        reference_symbols=(),
        required_timeframes=(CandleTimeframe.M1,),
        warmup_bars=31,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=CandleTimeframe.M1,
        parameter_schema=ParameterSchema(
            (ParameterSpec("opening_range_minutes", ParameterKind.INT, 30, 1, 120),)
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=(
            "SELL when a post-window completed close crosses below the low of the first "
            "n regular-session M1 bars beginning at 09:30 America/New_York."
        ),
        source=(
            "Toby Crabel, Day Trading with Short Term Price Patterns and Opening Range "
            "Breakout (1990); long-exit adaptation of the classic downside opening-range "
            "breakout."
        ),
        execution_limitations=(
            "Advisory exit of an owned long, not a short entry; regular US equity "
            "session and M1 bars only.",
            "Every opening-range minute must be present; the trigger bar must be "
            "adjacent to the prior bar and evaluate at or after the range completion.",
        ),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        minutes = cls.spec.parameter_schema.validate(params)["opening_range_minutes"]
        assert isinstance(minutes, int)
        return minutes + 1

    def evaluate_trigger(
        self, context: MarketContext, params: Mapping[str, object], state: Mapping[str, object]
    ) -> StrategyEvaluation:
        minutes = self.spec.parameter_schema.validate(params)["opening_range_minutes"]
        assert isinstance(minutes, int)
        bars = closed_bars(self.spec, context, minutes + 1)
        if bars is None:
            return insufficient(self.spec, context)
        reason = gate(self.spec, context, state)
        if reason is not None:
            return blocked(self.spec, context, reason)

        session_date = bars[-1].start.astimezone(_NEW_YORK).date()
        window_open = datetime.combine(session_date, _SESSION_OPEN, tzinfo=_NEW_YORK)
        window_end = window_open + timedelta(minutes=minutes)
        range_bars = tuple(bar for bar in bars if window_open <= bar.start < window_end)
        expected = tuple(window_open + timedelta(minutes=index) for index in range(minutes))
        if len(range_bars) != minutes or tuple(bar.start for bar in range_bars) != expected:
            return insufficient(self.spec, context)
        prior, latest = bars[-2:]
        if latest.start < window_end or prior.start < window_open or not adjacent(prior, latest):
            return insufficient(self.spec, context)
        range_low = min(bar.low for bar in range_bars)
        return finish(
            self.spec, context, latest, state, prior.close >= range_low and latest.close < range_low
        )
