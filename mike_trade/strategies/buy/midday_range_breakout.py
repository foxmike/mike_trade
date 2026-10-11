"""Breakout above a completed New York lunch range."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import time
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

STRATEGY_ID = "midday_range_breakout"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1
DEFAULT_RANGE_START = time(12, 0)
DEFAULT_RANGE_END = time(13, 0)
DEFAULT_ENTRY_END = time(15, 0)
WARMUP_BARS = 211

_SCHEMA = ParameterSchema((
    ParameterSpec("range_start", ParameterKind.TIME_OF_DAY, default=DEFAULT_RANGE_START,
                  min_value=time(10, 0), max_value=time(14, 0)),
    ParameterSpec("range_end", ParameterKind.TIME_OF_DAY, default=DEFAULT_RANGE_END,
                  min_value=time(10, 1), max_value=time(14, 30)),
    ParameterSpec("entry_end", ParameterKind.TIME_OF_DAY, default=DEFAULT_ENTRY_END,
                  min_value=time(10, 2), max_value=time(16, 0)),
))


def _params(values: Mapping[str, object]) -> tuple[time, time, time]:
    p = _SCHEMA.validate(values)
    start, end, entry_end = p["range_start"], p["range_end"], p["entry_end"]
    assert isinstance(start, time) and isinstance(end, time) and isinstance(entry_end, time)
    require_param(start < end < entry_end,
                  "range_start must precede range_end, which must precede entry_end",
                  "entry_end")
    return start, end, entry_end


_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID, version=STRATEGY_VERSION, side=StrategyRole.BUY,
    category="technical", sub_category="intraday_session", reference_symbols=(),
    required_timeframes=(TIMEFRAME,), warmup_bars=WARMUP_BARS,
    completed_bars_only=True, requires_volume=False,
    trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
    parameter_schema=_SCHEMA, persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=(
        "Freeze the high of the completed 12:00-13:00 ET lunch range (configurable). "
        "After the range, and before the configured entry cutoff, BUY when consecutive "
        "completed M1 closes cross from at-or-below to strictly above that high."
    ),
    source=(
        "TradingView open-source 'Lunch Range - Retracement Hunter' documents a "
        "12:00-13:00 ET consolidation range monitored during the afternoon: "
        "https://www.tradingview.com/script/ruTg53J8-Lunch-Range-Retracement-Hunter/"
    ),
    execution_limitations=(
        "Entry signal only; no sizing, stops, targets, or order submission.",
        "The whole lunch range and current session must be exact contiguous completed M1 bars.",
        "No signal on an NYSE early close if the configured entry window never opens.",
    ), reason_codes=REASON_CODES,
)


class MiddayRangeBreakoutBuy(BuyStrategy):
    spec = _SPEC

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        _, end, _ = _params(params)
        return minutes_from_open(end) + 1

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        range_start, range_end, entry_end = _params(params)
        reason = gate(self.spec, context, state)
        if reason is not None:
            return blocked(self.spec, context, reason)
        found = current_session_bars(context)
        if found is None or len(found[1]) < 2:
            return insufficient(self.spec, context)
        session, bars = found
        start_at = at(session.session_date, range_start)
        end_at = at(session.session_date, range_end)
        window = complete_window(bars, start_at, end_at)
        if window is None:
            return insufficient(self.spec, context)
        latest, prior = bars[-1], bars[-2]
        if not end_at <= latest.start < at(session.session_date, entry_end):
            return finish(self.spec, context, latest, state, False)
        level = max(bar.high for bar in window)
        return finish(self.spec, context, latest, state,
                      crossed_above(prior.close, level, latest.close, level))
