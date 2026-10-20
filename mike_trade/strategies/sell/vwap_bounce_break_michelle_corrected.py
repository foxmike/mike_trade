"""Michelle's corrected two-stage protective SELL (business rules 46-50).

Unlike the original Michelle SELL (an advisory mirror of an engine-owned trailing stop), this
strategy produces the MANDATORY protective exits of the corrected rules.  The engines classify
its signal as ``ExitSource.PROTECTIVE_STOP``: it outranks every discretionary exit, bypasses the
minimum-hold period and is never subject to review (a JEV gate, when integrated, must treat the
source as non-vetoable).

* Stage 1: ``Fill - $0.75`` fixed stop (broker/simulator-held; mirrored here).
* Stage 2: once the market has traded at ``Fill + $0.50`` the stop is breakeven (``Fill``) and the
  EMA9 trailing exit is armed; the whole position exits the instant ``price <= Fill`` or
  ``price < EMA9`` of the last COMPLETED candle - an intrabar breach, no candle-close wait.

The pure rule lives in :mod:`mike_trade.risk.michelle_corrected_stop`.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.risk import michelle_corrected_stop as stops
from mike_trade.scheduling.calendar import SessionStatus
from mike_trade.strategies import michelle_corrected as mc
from mike_trade.strategies.base import (
    ParameterSchema,
    ReasonCode,
    SellStrategy,
    StrategyEvaluation,
    StrategySpec,
    TriggerKind,
)
from mike_trade.strategies.michelle_common import (
    CORRECTED_SELL_ID,
    NEW_YORK,
    live_trade,
    safe_decimal,
    state_time,
)

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = CORRECTED_SELL_ID
STRATEGY_VERSION = "1.0.0"
STATE_KEYS = (
    "position_cycle_id",
    "position_fill_price",
    "position_fill_time",
    "highest_trade_price",
    "active_stop_mode",
    "active_stop_price",
    "stage_two_active",
    "exit_trigger",
    "last_signal_event",
)

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.SELL,
    category="technical",
    sub_category="vwap",
    reference_symbols=(),
    required_timeframes=(CandleTimeframe.M1,),
    warmup_bars=1,
    completed_bars_only=False,
    requires_volume=False,
    trigger_kind=TriggerKind.EDGE_ON_TRADE,
    trigger_timeframe=CandleTimeframe.M1,
    parameter_schema=ParameterSchema(()),
    persistent_state_keys=STATE_KEYS,
    methodology=(
        "Two-stage protection of a managed long. Stage 1: fixed stop at fill - $0.75. Stage 2: "
        "when the market trades at fill + $0.50 the stop becomes breakeven (never lowered) and the "
        "EMA9 trailing exit arms; the full position exits at market the moment price <= fill or "
        "price < EMA9 of the last completed one-minute candle, intrabar. No partial exits."
    ),
    source=(
        "Michelle, vwap_bounce_break_business_rules_final.docx rules 46-50 and 55; "
        "docs/strategies/michelle/vwap_bounce_break_michelle_corrected.md."
    ),
    execution_limitations=(
        "Classified as a PROTECTIVE_STOP exit by every engine: never vetoed, delayed or held.",
        "PAPER/LIVE: the broker holds only the fixed Stage 1 / breakeven stop; the EMA9 breach is "
        "application-managed on live trades, so protection against a process or feed outage is "
        "the broker-held stop only.",
        "OHLC replay applies Stage 2 from the candle after the one that reached fill + $0.50 and, "
        "when one candle spans both the initial stop and the activation level, the initial stop "
        "is assumed to trigger first (rule 55).",
    ),
    reason_codes=(
        ReasonCode.SIGNAL,
        ReasonCode.NO_SIGNAL,
        ReasonCode.INSUFFICIENT_DATA,
        ReasonCode.STALE_DATA,
        ReasonCode.NOT_ELIGIBLE,
    ),
)


class VwapBounceBreakMichelleCorrectedSell(SellStrategy):
    spec = _SPEC

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
        initial = stops.initial_stop(fill)
        if initial is None:
            return StrategyEvaluation.no_signal(self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,))
        day = context.as_of.astimezone(NEW_YORK).date()
        bars = mc.prepare_session(context.candles.get(CandleTimeframe.M1, ()), day)
        if not bars:
            return StrategyEvaluation.no_signal(self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,))
        fill_time = state_time(state.get("position_fill_time"))
        prior_high = safe_decimal(state.get("highest_trade_price")) or fill

        live = live_trade(context)
        if live is not None:
            price, event_time, developing = live
            ema9 = bars[-1].ema9  # EMA9 of the most recently COMPLETED candle
            high = max(prior_high, price)
            stage_two = stops.stage_two_reached(high, fill)
            observed = price
            bar_time = developing.start
            event_key = event_time.isoformat()
        else:
            latest = bars[-1]
            ema9 = bars[-2].ema9 if len(bars) >= 2 else None  # EMA9 in force while it traded
            # Stage 2 is known only from candles BEFORE this one (OHLC ordering is unknowable):
            # a candle that reaches the activation level and also the initial stop is a stop (55).
            before = max(
                [fill, *(b.high for b in bars[:-1] if fill_time is None or b.start >= fill_time)]
            )
            stage_two = stops.stage_two_reached(before, fill)
            observed = latest.low  # conservative: the adverse extreme can trigger
            bar_time = latest.start
            event_key = latest.end.isoformat()
            high = max(before, latest.high)  # the persisted high does include this candle

        trigger = stops.exit_trigger(observed, fill, ema9, stage_two=stage_two)
        mode = "BREAKEVEN" if stage_two else "FIXED"
        level = stops.effective_stop_level(fill, ema9, stage_two=stage_two)
        updates: dict[str, object] = {
            "position_cycle_id": cycle_id,
            "position_fill_price": str(fill),
            "highest_trade_price": str(high),
            "stage_two_active": "1" if stops.stage_two_reached(high, fill) else "",
            "active_stop_mode": mode,
            "active_stop_price": str(level if level is not None else initial),
        }
        if trigger is None or state.get("last_signal_event") == event_key:
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.NO_SIGNAL,), bar_time=bar_time,
                state_updates=updates,
            )
        updates["last_signal_event"] = event_key
        updates["exit_trigger"] = trigger.value
        reference = observed if live is not None else (level if level is not None else initial)
        return StrategyEvaluation.for_signal(
            self.spec,
            symbol,
            bar_time,
            reference_price=Decimal(reference),
            state_updates=updates,
        )
