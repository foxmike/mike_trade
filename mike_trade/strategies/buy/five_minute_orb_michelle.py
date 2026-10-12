"""Michelle's five-minute opening-range-breakout BUY strategy."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import time
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.scheduling.calendar import SessionStatus
from mike_trade.strategies.base import (
    BuyStrategy,
    ParameterKind,
    ParameterSchema,
    ParameterSpec,
    ReasonCode,
    StrategyEvaluation,
    StrategySpec,
    TriggerKind,
)
from mike_trade.strategies.closed_bar import ELIGIBILITY_WATCHING_BUY
from mike_trade.strategies.michelle_common import (
    NEW_YORK,
    adjacent,
    current_session_vwap,
    daily_wilder_atr,
    live_trade,
    regular_session_bars,
    safe_decimal,
    session_vwaps,
)

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "five_minute_orb_michelle_buy"
STRATEGY_VERSION = "1.0.0"
STATE_KEYS = (
    "session_id",
    "orb_low_breached",
    "last_trade_price",
    "last_signal_event",
    "signal_cycle_id",
    "entry_vwap",
    "entry_orb_high",
    "entry_reference_price",
    "signal_event_time",
    "position_cycle_id",
    "position_fill_price",
    "position_fill_time",
    "attempt_times",
)

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.BUY,
    category="technical",
    sub_category="breakout",
    reference_symbols=(),
    required_timeframes=(CandleTimeframe.M1, CandleTimeframe.D1),
    history_bars=((CandleTimeframe.D1, 15),),
    warmup_bars=5,
    completed_bars_only=False,
    requires_volume=True,
    trigger_kind=TriggerKind.EDGE_ON_TRADE,
    trigger_timeframe=CandleTimeframe.M1,
    parameter_schema=ParameterSchema(
        (
            ParameterSpec(
                "spy_confirmation_enabled",
                ParameterKind.BOOL,
                default=False,
                description=(
                    "When enabled, the latest completed SPY M1 close must exceed its prior close."
                ),
            ),
        )
    ),
    persistent_state_keys=STATE_KEYS,
    methodology=(
        "Michelle's first-five-minute ORB: require a green 09:30-09:35 range of "
        "0.30%-1.80%, opening close above regular-session VWAP, rising completed-minute "
        "VWAP, then buy the first qualifying price cross above ORB high before 11:30 ET. "
        "Daily Wilder ATR(14) limits VWAP extension and ORB-high-minus-$0.25 limits risk."
    ),
    source="Michelle, Five_Minute_ORB_Formatted.docx, plus approved operator clarifications.",
    execution_limitations=(
        "Live mode uses ordered trade events and the developing M1 candle; historical mode uses "
        "a conservative completed-M1 crossing and fills at the next eligible bar open.",
        "Advisory MARKET-entry signal only; sizing, submission and protection are engine-owned.",
        "Optional SPY confirmation requires spy_close and spy_close_prev in context indicators.",
    ),
    reason_codes=(
        ReasonCode.SIGNAL,
        ReasonCode.NO_SIGNAL,
        ReasonCode.INSUFFICIENT_DATA,
        ReasonCode.STALE_DATA,
        ReasonCode.NOT_ELIGIBLE,
    ),
)


class FiveMinuteOrbMichelleBuy(BuyStrategy):
    """Event-driven live ORB entry with an approved completed-minute replay path."""

    spec = _SPEC

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        validated = self.spec.parameter_schema.validate(params)
        symbol = context.symbol
        if context.freshness.stale:
            return StrategyEvaluation.no_signal(self.spec, symbol, (ReasonCode.STALE_DATA,))
        if context.session_status is not SessionStatus.REGULAR:
            return StrategyEvaluation.no_signal(self.spec, symbol, (ReasonCode.NOT_ELIGIBLE,))
        if state.get("eligibility") != ELIGIBILITY_WATCHING_BUY:
            return StrategyEvaluation.no_signal(self.spec, symbol, (ReasonCode.NOT_ELIGIBLE,))

        bars = regular_session_bars(context.candles.get(CandleTimeframe.M1, ()), context.session_id)
        if len(bars) < 5:
            return StrategyEvaluation.no_signal(self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,))
        opening = bars[:5]
        if opening[0].start.astimezone(NEW_YORK).time() != time(9, 30) or not adjacent(opening):
            return StrategyEvaluation.no_signal(self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,))
        vwaps = session_vwaps(bars)
        if len(vwaps) < 5 or vwaps[4] is None:
            return StrategyEvaluation.no_signal(self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,))

        opening_price = opening[0].open
        orb_high = max(bar.high for bar in opening)
        orb_low = min(bar.low for bar in opening)
        orb_close = opening[-1].close
        height = orb_high - orb_low
        setup_valid = (
            orb_close > opening_price
            and Decimal("0.0030") * opening_price <= height <= Decimal("0.0180") * opening_price
            and orb_close > vwaps[4]
        )
        if not setup_valid:
            return StrategyEvaluation.no_signal(self.spec, symbol, (ReasonCode.NO_SIGNAL,))

        session_key = context.session_id or opening[0].start.astimezone(NEW_YORK).date().isoformat()
        same_session = state.get("session_id") == session_key
        breached = bool(state.get("orb_low_breached")) if same_session else False
        closed_after_open = bars[5:]
        breached = breached or any(bar.low < orb_low for bar in closed_after_open)
        updates: dict[str, object] = {
            "session_id": session_key,
            "orb_low_breached": breached,
        }

        live = live_trade(context)
        if live is not None:
            price, event_time, developing = live
            local_event = event_time.astimezone(NEW_YORK)
            if developing.low < orb_low or price < orb_low:
                updates["orb_low_breached"] = True
                breached = True
            prior_trade = safe_decimal(state.get("last_trade_price")) if same_session else None
            crossing_base = developing.open if prior_trade is None else prior_trade
            crossed = crossing_base <= orb_high and price > orb_high
            green = price > developing.open
            updates["last_trade_price"] = str(price)
            bar_time = event_time
            reference_price = price
        else:
            latest = bars[-1]
            event_time = latest.end
            local_event = event_time.astimezone(NEW_YORK)
            if len(bars) < 6:
                return StrategyEvaluation.no_signal(
                    self.spec,
                    symbol,
                    (ReasonCode.INSUFFICIENT_DATA,),
                    state_updates=updates,
                )
            prior = bars[-2]
            crossed = prior.close <= orb_high and latest.high > orb_high
            green = latest.close > latest.open
            bar_time = latest.start
            reference_price = latest.close

        if not time(9, 35) <= local_event.time() < time(11, 30):
            return StrategyEvaluation.no_signal(
                self.spec,
                symbol,
                (ReasonCode.NOT_ELIGIBLE,),
                bar_time=bar_time,
                state_updates=updates,
            )
        if breached or not crossed or not green:
            return StrategyEvaluation.no_signal(
                self.spec,
                symbol,
                (ReasonCode.NO_SIGNAL,),
                bar_time=bar_time,
                state_updates=updates,
            )
        if len(vwaps) < 2 or vwaps[-1] is None or vwaps[-2] is None:
            return StrategyEvaluation.no_signal(
                self.spec,
                symbol,
                (ReasonCode.INSUFFICIENT_DATA,),
                bar_time=bar_time,
                state_updates=updates,
            )
        current_vwap = current_session_vwap(context, bars) if live is not None else vwaps[-1]
        assert current_vwap is not None and vwaps[-1] is not None and vwaps[-2] is not None
        if reference_price <= current_vwap or vwaps[-1] <= vwaps[-2]:
            return StrategyEvaluation.no_signal(
                self.spec,
                symbol,
                (ReasonCode.NO_SIGNAL,),
                bar_time=bar_time,
                state_updates=updates,
            )
        if validated["spy_confirmation_enabled"]:
            spy_now = context.indicators.get("spy_close")
            spy_prior = context.indicators.get("spy_close_prev")
            if spy_now is None or spy_prior is None:
                return StrategyEvaluation.no_signal(
                    self.spec,
                    symbol,
                    (ReasonCode.INSUFFICIENT_DATA,),
                    bar_time=bar_time,
                    state_updates=updates,
                )
            if spy_now <= spy_prior:
                return StrategyEvaluation.no_signal(
                    self.spec,
                    symbol,
                    (ReasonCode.NO_SIGNAL,),
                    bar_time=bar_time,
                    state_updates=updates,
                )
        atr = daily_wilder_atr(context.candles.get(CandleTimeframe.D1, ()))
        if atr is None:
            return StrategyEvaluation.no_signal(
                self.spec,
                symbol,
                (ReasonCode.INSUFFICIENT_DATA,),
                bar_time=bar_time,
                state_updates=updates,
            )
        initial_stop = orb_high - Decimal("0.25")
        risk = reference_price - initial_stop
        if (
            reference_price > current_vwap + Decimal("1.5") * atr
            or risk < Decimal(0)
            or risk > Decimal("0.01") * reference_price
        ):
            return StrategyEvaluation.no_signal(
                self.spec,
                symbol,
                (ReasonCode.NO_SIGNAL,),
                bar_time=bar_time,
                state_updates=updates,
            )
        event_key = event_time.isoformat()
        if state.get("last_signal_event") == event_key:
            return StrategyEvaluation.no_signal(
                self.spec,
                symbol,
                (ReasonCode.NO_SIGNAL,),
                bar_time=bar_time,
                state_updates=updates,
            )
        cycle_id = f"{session_key}:orb:{event_key}"
        updates.update(
            {
                "last_signal_event": event_key,
                "signal_cycle_id": cycle_id,
                "entry_vwap": str(current_vwap),
                "entry_orb_high": str(orb_high),
                "entry_reference_price": str(reference_price),
                "signal_event_time": event_key,
            }
        )
        return StrategyEvaluation.for_signal(
            self.spec,
            symbol,
            bar_time,
            reference_price=reference_price,
            state_updates=updates,
        )
