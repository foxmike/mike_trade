"""Current regular-session high-of-day momentum breakout BUY."""

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
from mike_trade.strategies.buy._session import at, current_session_bars, minutes_from_open
from mike_trade.strategies.closed_bar import (
    REASON_CODES,
    STATE_LAST_SIGNAL_BAR,
    blocked,
    finish,
    gate,
    insufficient,
    require_param,
)

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "high_of_day_momentum"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1
DEFAULT_ENTRY_START = time(10, 0)
DEFAULT_ENTRY_END = time(15, 30)
WARMUP_BARS = 31

_SCHEMA = ParameterSchema((
    ParameterSpec("entry_start", ParameterKind.TIME_OF_DAY, default=DEFAULT_ENTRY_START,
                  min_value=time(9, 31), max_value=time(15, 29),
                  description="Earliest New York wall-clock start time for a signal bar."),
    ParameterSpec("entry_end", ParameterKind.TIME_OF_DAY, default=DEFAULT_ENTRY_END,
                  min_value=time(9, 32), max_value=time(16, 0),
                  description="Exclusive New York wall-clock end of the entry window."),
))


def _params(values: Mapping[str, object]) -> tuple[time, time]:
    p = _SCHEMA.validate(values)
    start, end = p["entry_start"], p["entry_end"]
    assert isinstance(start, time) and isinstance(end, time)
    require_param(start < end, "entry_start must precede entry_end", "entry_end")
    return start, end


_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID, version=STRATEGY_VERSION, side=StrategyRole.BUY,
    category="technical", sub_category="momentum", reference_symbols=(),
    required_timeframes=(TIMEFRAME,), warmup_bars=WARMUP_BARS,
    completed_bars_only=True, requires_volume=False,
    trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
    parameter_schema=_SCHEMA, persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=(
        "During the configured regular-session entry window, BUY when the latest "
        "completed M1 close is strictly above every earlier high of the current session."
    ),
    source=(
        "Warrior Trading, Day Trading Guide, 'High of Day Momentum' scanner/setup: "
        "https://media.warriortrading.com/2021/05/05144410/"
        "Warrior-Trading-Day-Trading-Guide-v2.pdf"
    ),
    execution_limitations=(
        "Entry signal only; no sizing, stops, targets, or order submission.",
        "Uses only highs observed before the triggering completed bar; no eventual-day high.",
        "Requires an exact, contiguous current regular session from 09:30 ET.",
    ), reason_codes=REASON_CODES,
)


class HighOfDayMomentumBuy(BuyStrategy):
    spec = _SPEC

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        start, _ = _params(params)
        return minutes_from_open(start) + 1

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        start, end = _params(params)
        reason = gate(self.spec, context, state)
        if reason is not None:
            return blocked(self.spec, context, reason)
        found = current_session_bars(context)
        if found is None or len(found[1]) < 2:
            return insufficient(self.spec, context)
        session, bars = found
        latest = bars[-1]
        if not at(session.session_date, start) <= latest.start < at(session.session_date, end):
            return finish(self.spec, context, latest, state, False)
        prior_high = max(bar.high for bar in bars[:-1])
        return finish(self.spec, context, latest, state, latest.close > prior_high)
