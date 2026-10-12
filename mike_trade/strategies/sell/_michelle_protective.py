"""Shared fixed-then-trailing SELL evaluator for Michelle strategies."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING, ClassVar

from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.scheduling.calendar import SessionStatus
from mike_trade.strategies.base import ReasonCode, SellStrategy, StrategyEvaluation, StrategySpec
from mike_trade.strategies.michelle_common import live_trade, safe_decimal

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext


class MichelleProtectiveSell(SellStrategy):
    """Advisory mirror of the engine-owned fixed-to-trailing protection state."""

    spec: ClassVar[StrategySpec]
    reference_state_key: ClassVar[str]

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        self.spec.parameter_schema.validate(params)
        symbol = context.symbol
        if context.freshness.stale:
            return StrategyEvaluation.no_signal(self.spec, symbol, (ReasonCode.STALE_DATA,))
        if context.session_status is not SessionStatus.REGULAR:
            return StrategyEvaluation.no_signal(self.spec, symbol, (ReasonCode.NOT_ELIGIBLE,))
        reference = safe_decimal(state.get(self.reference_state_key))
        fill = safe_decimal(state.get("position_fill_price"))
        cycle_id = state.get("position_cycle_id")
        if reference is None or fill is None or not isinstance(cycle_id, str) or not cycle_id:
            return StrategyEvaluation.no_signal(self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,))
        live = live_trade(context)
        if live is not None:
            current, event_time, _developing = live
            favorable = current
            bar_time = event_time
        else:
            bars = context.candles.get(CandleTimeframe.M1, ())
            if not bars:
                return StrategyEvaluation.no_signal(
                    self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
                )
            latest = bars[-1]
            current = latest.low  # conservative bar replay: adverse extreme can trigger
            favorable = latest.high
            event_time = latest.end
            bar_time = latest.start

        prior_high = safe_decimal(state.get("highest_trade_price")) or fill
        high = max(prior_high, favorable)
        initial = reference - Decimal("0.25")
        if initial <= Decimal(0):
            return StrategyEvaluation.no_signal(self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,))
        prior_stop = safe_decimal(state.get("active_stop_price")) or initial
        candidate = high * Decimal("0.995")
        active = state.get("active_stop_mode") == "TRAILING"
        if candidate > reference + Decimal("0.25"):
            active = True
        stop = max(prior_stop, candidate) if active else initial
        updates: dict[str, object] = {
            "position_cycle_id": cycle_id,
            "position_fill_price": str(fill),
            self.reference_state_key: str(reference),
            "highest_trade_price": str(high),
            "active_stop_mode": "TRAILING" if active else "FIXED",
            "active_stop_price": str(stop),
        }
        triggered = current <= stop
        event_key = event_time.isoformat()
        if not triggered or state.get("last_signal_event") == event_key:
            return StrategyEvaluation.no_signal(
                self.spec,
                symbol,
                (ReasonCode.NO_SIGNAL,),
                bar_time=bar_time,
                state_updates=updates,
            )
        updates["last_signal_event"] = event_key
        return StrategyEvaluation.for_signal(
            self.spec,
            symbol,
            bar_time,
            reference_price=stop,
            state_updates=updates,
        )
