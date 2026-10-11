"""Bullish final-hour breakout above the pre-power-hour high."""

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
)

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "power_hour_momentum"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1
DEFAULT_POWER_HOUR_START = time(15, 0)
WARMUP_BARS = 331

_SCHEMA = ParameterSchema((ParameterSpec(
    "power_hour_start", ParameterKind.TIME_OF_DAY, default=DEFAULT_POWER_HOUR_START,
    min_value=time(14, 0), max_value=time(15, 30),
    description="New York wall-clock start of the final-session momentum window.",
),))

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID, version=STRATEGY_VERSION, side=StrategyRole.BUY,
    category="technical", sub_category="intraday_session", reference_symbols=(),
    required_timeframes=(TIMEFRAME,), warmup_bars=WARMUP_BARS,
    completed_bars_only=True, requires_volume=False,
    trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
    parameter_schema=_SCHEMA, persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=(
        "At and after power_hour_start, require bullish session direction (the last "
        "pre-window close exceeds the regular-session open), then BUY when consecutive "
        "completed M1 closes cross above the frozen pre-window high-of-day."
    ),
    source=(
        "Edgeful Help Center, 'How to read and trade the power hour reports (breakout + "
        "continuation)': a fresh final-hour high aligned with the day's continuation "
        "direction: https://help.edgeful.com/en/articles/16100944-how-to-read-and-trade-"
        "the-power-hour-reports-breakout-continuation"
    ),
    execution_limitations=(
        "Entry signal only; no sizing, stops, targets, or order submission.",
        "Requires exact contiguous current-session M1 bars from 09:30 ET.",
        "NYSE early-close sessions cannot reach the default 15:00 ET window.",
    ), reason_codes=REASON_CODES,
)


class PowerHourMomentumBuy(BuyStrategy):
    spec = _SPEC

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        value = cls.spec.parameter_schema.validate(params)["power_hour_start"]
        assert isinstance(value, time)
        return minutes_from_open(value) + 1

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        start = self.spec.parameter_schema.validate(params)["power_hour_start"]
        assert isinstance(start, time)
        reason = gate(self.spec, context, state)
        if reason is not None:
            return blocked(self.spec, context, reason)
        found = current_session_bars(context)
        if found is None or len(found[1]) < 2:
            return insufficient(self.spec, context)
        session, bars = found
        start_at = at(session.session_date, start)
        pre_window = complete_window(bars, session.open_utc, start_at)
        if pre_window is None:
            return insufficient(self.spec, context)
        latest, prior = bars[-1], bars[-2]
        if not start_at <= latest.start < session.close_utc:
            return finish(self.spec, context, latest, state, False)
        bullish_day = pre_window[-1].close > pre_window[0].open
        level = max(bar.high for bar in pre_window)
        triggered = bullish_day and crossed_above(prior.close, level, latest.close, level)
        return finish(self.spec, context, latest, state, triggered)
