"""Delayed afternoon breakout of the completed first-hour range."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import time, timedelta
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
    at,
    complete_window,
    current_session_bars,
    minutes_from_open,
)
from mike_trade.strategies.closed_bar import (
    REASON_CODES,
    STATE_LAST_SIGNAL_BAR,
    blocked,
    crossed_above,
    finish,
    gate,
    insufficient,
    require_param,
)

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "afternoon_breakout"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1
DEFAULT_MORNING_RANGE_MINUTES = 60
DEFAULT_ENTRY_START = time(13, 0)
DEFAULT_ENTRY_END = time(15, 0)
WARMUP_BARS = 211

_SCHEMA = ParameterSchema((
    ParameterSpec("morning_range_minutes", ParameterKind.INT,
                  default=DEFAULT_MORNING_RANGE_MINUTES, min_value=30, max_value=120),
    ParameterSpec("entry_start", ParameterKind.TIME_OF_DAY, default=DEFAULT_ENTRY_START,
                  min_value=time(11, 0), max_value=time(14, 59)),
    ParameterSpec("entry_end", ParameterKind.TIME_OF_DAY, default=DEFAULT_ENTRY_END,
                  min_value=time(11, 1), max_value=time(16, 0)),
))


def _params(values: Mapping[str, object]) -> tuple[int, time, time]:
    p = _SCHEMA.validate(values)
    length, start, end = p["morning_range_minutes"], p["entry_start"], p["entry_end"]
    assert isinstance(length, int) and isinstance(start, time) and isinstance(end, time)
    require_param(start < end, "entry_start must precede entry_end", "entry_end")
    require_param(length <= minutes_from_open(start),
                  "morning range must complete before entry_start", "entry_start")
    return length, start, end


_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID, version=STRATEGY_VERSION, side=StrategyRole.BUY,
    category="technical", sub_category="intraday_session", reference_symbols=(),
    required_timeframes=(TIMEFRAME,), warmup_bars=WARMUP_BARS,
    completed_bars_only=True, requires_volume=False,
    trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
    parameter_schema=_SCHEMA, persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=(
        "Freeze the high of the first 60 regular-session minutes (configurable). During "
        "the configured 13:00-15:00 ET afternoon window, BUY when consecutive completed "
        "M1 closes cross from at-or-below to strictly above that morning high."
    ),
    source=(
        "Alan S. Farley, The Master Swing Trader (McGraw-Hill, 2001), first-hour "
        "support/resistance and mid-afternoon breakout setup."
    ),
    execution_limitations=(
        "Entry signal only; no sizing, stops, targets, or order submission.",
        "Requires exact contiguous M1 history from the NYSE open through the signal bar.",
        "The morning reference is frozen before the afternoon entry window.",
    ), reason_codes=REASON_CODES,
)


class AfternoonBreakoutBuy(BuyStrategy):
    spec = _SPEC

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        _, start, _ = _params(params)
        return minutes_from_open(start) + 1

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        length, entry_start, entry_end = _params(params)
        reason = gate(self.spec, context, state)
        if reason is not None:
            return blocked(self.spec, context, reason)
        found = current_session_bars(context)
        if found is None or len(found[1]) < 2:
            return insufficient(self.spec, context)
        session, bars = found
        range_end = session.open_utc + timedelta(minutes=length)
        window = complete_window(bars, session.open_utc, range_end)
        if window is None:
            return insufficient(self.spec, context)
        latest, prior = bars[-1], bars[-2]
        entry_start_at = at(session.session_date, entry_start)
        entry_end_at = at(session.session_date, entry_end)
        if not entry_start_at <= latest.start < entry_end_at:
            return finish(self.spec, context, latest, state, False)
        level = max(bar.high for bar in window)
        return finish(self.spec, context, latest, state,
                      crossed_above(prior.close, level, latest.close, level))
