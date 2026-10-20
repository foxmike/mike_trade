"""Michelle's corrected VWAP Bounce/Break BUY (business rules 1-45, 53-55).

Independent of ``vwap_bounce_break_michelle_buy`` (which is left untouched).  All trading
logic lives in :mod:`mike_trade.strategies.michelle_corrected`; this class adapts it to the
strategy API: completed-candle setup state, an intrabar entry window evaluated on trade ticks
in SHADOW/PAPER/LIVE, and an OHLC emulation of that window for historical replay.

Persisted state is only a purge *barrier* (``consumed_after`` / ``purge_barrier``): the setup is a
deterministic replay of the completed candles after the barrier, so a restart can never resurrect
a purged or consumed setup, and a new trade needs a completely new setup cycle (Rules 40, 53-54).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.risk import michelle_corrected_stop as stops
from mike_trade.scheduling.calendar import SessionStatus
from mike_trade.strategies import michelle_corrected as mc
from mike_trade.strategies.base import (
    BuyStrategy,
    ParameterSchema,
    ReasonCode,
    StrategyEvaluation,
    StrategySpec,
    TriggerKind,
)
from mike_trade.strategies.closed_bar import ELIGIBILITY_WATCHING_BUY
from mike_trade.strategies.michelle_common import (
    CORRECTED_BUY_ID,
    NEW_YORK,
    live_trade,
    state_time,
)

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = CORRECTED_BUY_ID
STRATEGY_VERSION = "1.0.0"
REFERENCE_SYMBOL = "QQQ"
STATE_KEYS = (
    "session_id",
    "consumed_after",
    "purge_barrier",
    "was_held",
    "last_signal_event",
    "signal_cycle_id",
    "entry_vwap",
    "entry_reference_price",
    "signal_event_time",
    "entry_mode",
    "entry_price_hint",
    "entry_time_hint",
    "entry_evidence",
    "position_cycle_id",
    "position_fill_price",
    "position_fill_time",
    "position_vwap_at_entry",
)

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.BUY,
    category="technical",
    sub_category="vwap",
    reference_symbols=(REFERENCE_SYMBOL,),
    required_timeframes=(CandleTimeframe.M1,),
    warmup_bars=1,
    completed_bars_only=False,
    requires_volume=True,
    trigger_kind=TriggerKind.EDGE_ON_TRADE,
    trigger_timeframe=CandleTimeframe.M1,
    parameter_schema=ParameterSchema(()),
    persistent_state_keys=STATE_KEYS,
    methodology=(
        "Corrected VWAP bounce (55 rules). A completed candle closing below VWAP, EMA9 and EMA20 "
        "(both EMAs below VWAP) starts a setup; its low may not fall more than $3.00 below the VWAP "
        "of the candle that printed it. A hammer, 3-6 candle support shelf (lows within $0.50) or "
        "bullish engulfing below VWAP (a lower Pattern 2 once a lower low prints), three straight "
        "closes below VWAP and then the first green candle that touches VWAP and closes above it "
        "within 30 minutes confirm. Entry is a market buy during the next five candles when price "
        "is at/above the confirmation close, strictly above VWAP and at most VWAP+$0.50, if EMA "
        "momentum, the QQQ filter and the VWAP-crossing filter pass. One setup, one trade."
    ),
    source=(
        "Michelle, vwap_bounce_break_business_rules_final.docx (55 numbered business rules), "
        "docs/strategies/michelle/vwap_bounce_break_michelle_corrected.md."
    ),
    execution_limitations=(
        "SHADOW/PAPER/LIVE evaluate the entry window on every trade tick and send a market buy; "
        "the broker/adapter fill can differ from the trigger print by slippage.",
        "OHLC replay cannot see intrabar ordering: the C+1 open leg is exact, the deferred "
        "trigger fills at Confirmation_Close stamped at the bar end only when the VWAP band holds "
        "at both bounds of that bar's VWAP; other intrabar paths are never guessed into fills.",
        "Premarket candles must be present in the context to seed EMA9/EMA20; without them the "
        "strategy cannot start a setup (fails closed).",
        "The QQQ filter needs QQQ one-minute candles in MarketContext.reference_candles.",
        "Account-level $150 daily loss and 15:55 / early-close liquidation are enforced by the "
        "shared risk layer, not by this strategy.",
    ),
    reason_codes=(
        ReasonCode.SIGNAL,
        ReasonCode.NO_SIGNAL,
        ReasonCode.INSUFFICIENT_DATA,
        ReasonCode.STALE_DATA,
        ReasonCode.NOT_ELIGIBLE,
    ),
)


def _barrier(state: Mapping[str, object], session_key: str) -> datetime | None:
    """The latest purge / consumption instant of *this* session (earlier candles are unusable)."""
    if state.get("session_id") != session_key:
        return None
    stamps = [state_time(state.get("consumed_after")), state_time(state.get("purge_barrier"))]
    known = [stamp for stamp in stamps if stamp is not None]
    return max(known) if known else None


class VwapBounceBreakMichelleCorrectedBuy(BuyStrategy):
    """Stateful BUY: explicit setup state machine plus intrabar entry window."""

    spec = _SPEC

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        self.spec.parameter_schema.validate(params)
        symbol = context.symbol
        if context.freshness.stale:  # Rule 6
            return StrategyEvaluation.no_signal(self.spec, symbol, (ReasonCode.STALE_DATA,))
        if context.session_status is not SessionStatus.REGULAR:  # Rule 2
            return StrategyEvaluation.no_signal(self.spec, symbol, (ReasonCode.NOT_ELIGIBLE,))
        day = context.as_of.astimezone(NEW_YORK).date()
        session_key = day.isoformat()
        bars = mc.prepare_session(context.candles.get(CandleTimeframe.M1, ()), day)
        live = live_trade(context)
        developing = live[2] if live is not None else None
        updates: dict[str, object] = {"session_id": session_key}
        if state.get("session_id") != session_key:  # a new trading day starts clean
            updates.update({"consumed_after": "", "purge_barrier": "", "was_held": ""})
        if state.get("eligibility") != ELIGIBILITY_WATCHING_BUY:
            # Rule 6 / 53: a held position keeps the setup purged; its exit is the purge instant.
            stamp = developing.end if developing is not None else (bars[-1].end if bars else None)
            updates["was_held"] = "1"
            if stamp is not None:
                updates["purge_barrier"] = stamp.isoformat()
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.NOT_ELIGIBLE,), state_updates=updates
            )
        if not bars:
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,), state_updates=updates
            )
        barrier = _barrier(state, session_key)
        if state.get("was_held") == "1" and state.get("session_id") == session_key:
            # first flat evaluation after an exit: the candle containing the exit is pre-purge
            stamp = developing.end if developing is not None else bars[-1].end
            barrier = max(barrier, stamp) if barrier is not None else stamp
            updates.update({"purge_barrier": stamp.isoformat(), "was_held": ""})

        qqq_all = mc.prepare_session(
            (context.reference_candles or {}).get(REFERENCE_SYMBOL, ()), day
        )
        qqq_now = (context.reference_prices or {}).get(REFERENCE_SYMBOL)

        if live is not None:
            price, event_time, candle = live
            snapshot = mc.scan(bars, barrier)
            vwap_now = mc.vwap_with(bars, candle)
            if vwap_now is None:
                return StrategyEvaluation.no_signal(
                    self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,), state_updates=updates
                )
            if mc.live_dip_breached(snapshot, candle, vwap_now):  # Rule 11, continuously
                updates["purge_barrier"] = candle.end.isoformat()
                return StrategyEvaluation.no_signal(
                    self.spec, symbol, (ReasonCode.NO_SIGNAL,), bar_time=candle.start,
                    state_updates=updates,
                )
            quote = context.latest_quote
            ask = quote.ask if quote is not None and not context.freshness.stale else None
            decision = mc.live_entry(
                snapshot, bars, candle, price, event_time, vwap_now, ask, qqq_all, qqq_now
            )
            bar_time = candle.start
            snapshot_facts = snapshot.facts()
        else:
            latest = bars[-1]
            before = mc.scan(bars[:-1], barrier)
            qqq_bars = [bar for bar in qqq_all if bar.end <= latest.start]
            qqq_price = qqq_bars[-1].close if qqq_bars else None
            decision = mc.bar_entry(before, bars[:-1], latest, qqq_bars, qqq_price)
            bar_time = latest.start
            snapshot_facts = before.facts()

        # Rule 46: the Stage 1 stop must be establishable at the fill, so a price that admits no
        # valid stop below it (at or under $0.75) can never be entered.
        if decision.enter and (
            decision.price is None or stops.initial_stop(decision.price) is None
        ):
            decision = mc.EntryDecision(False, reason="NO_VALID_INITIAL_STOP")
        if not decision.enter or decision.price is None or decision.at is None:
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.NO_SIGNAL,), bar_time=bar_time,
                state_updates=updates,
            )
        event_key = f"{decision.mode}:{decision.at.isoformat()}"
        if state.get("last_signal_event") == event_key:
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.NO_SIGNAL,), bar_time=bar_time,
                state_updates=updates,
            )
        evidence = {**snapshot_facts, **decision.facts, "decision": decision.reason}
        updates.update(
            {
                "last_signal_event": event_key,
                "signal_cycle_id": f"{symbol}:{bar_time.isoformat()}:corrected-vwap-cycle",
                "entry_vwap": str(decision.vwap),
                "entry_reference_price": str(decision.price),
                "signal_event_time": decision.at.isoformat(),
                "entry_mode": decision.mode.value if decision.mode is not None else "",
                "entry_price_hint": str(decision.price),
                "entry_time_hint": decision.at.isoformat(),
                "entry_evidence": json.dumps(evidence, sort_keys=True),
            }
        )
        return StrategyEvaluation.for_signal(
            self.spec,
            symbol,
            bar_time,
            reference_price=Decimal(decision.price),
            state_updates=updates,
        )
