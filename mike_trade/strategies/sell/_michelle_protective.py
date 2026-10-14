"""Shared fixed-then-trailing SELL evaluator for Michelle strategies.

Fill-based rule (Michelle's confirmed clarification): ``InitialStop = fill - $0.25`` (rounded up
to the tick), then a 0.2% trail of the highest traded price since the fill, activated only when
the tick-adjusted candidate strictly exceeds the initial stop. See ``risk.michelle_stop``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, ClassVar

from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.review.evidence import (
    EVIDENCE_INTERFACE_VERSION,
    DecisionEvidence,
    make_evidence,
)
from mike_trade.risk import michelle_stop
from mike_trade.scheduling.calendar import SessionStatus
from mike_trade.strategies.base import ReasonCode, SellStrategy, StrategyEvaluation, StrategySpec
from mike_trade.strategies.michelle_common import live_trade, safe_decimal

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext


class MichelleProtectiveSell(SellStrategy):
    """Advisory mirror of the engine-owned fixed-to-trailing protection state."""

    spec: ClassVar[StrategySpec]
    EVIDENCE_INTERFACE_VERSION: ClassVar[int] = EVIDENCE_INTERFACE_VERSION
    #: These signals mirror the engine-owned protective stop: a mandatory exit that decision
    #: review never sees (see ``mike_trade.review.stage``).
    PROTECTIVE_EXIT: ClassVar[bool] = True

    def build_evidence(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
        evaluation: StrategyEvaluation,
    ) -> DecisionEvidence:
        """Describe the protective-stop state (documentation only: never reviewed)."""
        fill = safe_decimal(state.get("position_fill_price"))
        initial = None if fill is None else michelle_stop.initial_stop(fill)
        return make_evidence(
            self.spec, evaluation, context,
            reason_code="PROTECTIVE_STOP",
            reason="the market traded at or through the active protective stop",
            evidence={
                "mandatory_exit": True,
                "position_cycle_id": state.get("position_cycle_id"),
                "position_fill_price": fill,
                "initial_stop": initial,
                "highest_trade_price": safe_decimal(state.get("highest_trade_price")),
                "active_stop_price": safe_decimal(state.get("active_stop_price")),
                "active_stop_mode": state.get("active_stop_mode"),
                "trail_percent": michelle_stop.MICHELLE_TRAIL_PERCENT,
            },
        )

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
        fill = safe_decimal(state.get("position_fill_price"))
        cycle_id = state.get("position_cycle_id")
        if fill is None or not isinstance(cycle_id, str) or not cycle_id:
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

        initial = michelle_stop.initial_stop(fill)
        if initial is None:
            return StrategyEvaluation.no_signal(self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,))
        # The highest traded price starts at the fill: earlier market highs are never included.
        prior_high = safe_decimal(state.get("highest_trade_price")) or fill
        high = max(prior_high, favorable)
        prior_stop = safe_decimal(state.get("active_stop_price")) or initial
        stop = michelle_stop.next_stop(initial, prior_stop, high)
        updates: dict[str, object] = {
            "position_cycle_id": cycle_id,
            "position_fill_price": str(fill),
            "highest_trade_price": str(high),
            "active_stop_mode": "TRAILING" if stop > initial else "FIXED",
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
