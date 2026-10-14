"""Michelle's stateful VWAP bounce/break BUY strategy."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import Candle, CandleTimeframe
from mike_trade.review.evidence import (
    EVIDENCE_INTERFACE_VERSION,
    DecisionEvidence,
    make_evidence,
)
from mike_trade.scheduling.calendar import SessionStatus
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
    NEW_YORK,
    current_session_vwap,
    ema_values,
    live_trade,
    regular_session_bars,
    safe_decimal,
    session_vwaps,
    state_time,
)

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "vwap_bounce_break_michelle_buy"
STRATEGY_VERSION = "1.0.0"
STATE_KEYS = (
    "session_id",
    "cycle_id",
    "breakdown_time",
    "cycle_low",
    "cycle_low_vwap",
    "bottom_pattern",
    "first_confirmation_time",
    "first_confirmation_close",
    "last_signal_event",
    "consumed_after",
    "signal_cycle_id",
    "entry_vwap",
    "entry_reference_price",
    "signal_event_time",
    "position_cycle_id",
    "position_fill_price",
    "position_fill_time",
    "position_vwap_at_entry",
    "attempt_times",
)

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.BUY,
    category="technical",
    sub_category="vwap",
    reference_symbols=(),
    required_timeframes=(CandleTimeframe.M1,),
    warmup_bars=20,
    completed_bars_only=False,
    requires_volume=True,
    trigger_kind=TriggerKind.EDGE_ON_TRADE,
    trigger_timeframe=CandleTimeframe.M1,
    parameter_schema=ParameterSchema(()),
    persistent_state_keys=STATE_KEYS,
    methodology=(
        "Michelle's two-stage VWAP recovery. A completed-bar EMA9<EMA20<VWAP breakdown "
        "must form a qualifying below-VWAP hammer, three-bar base, or bullish engulfing "
        "within a $2 dip. Within 30 minutes a first green candle crosses and closes above "
        "VWAP with improving EMA9 slope/spread. A later green candle enters at/above that "
        "confirmation close while no more than $0.50 above VWAP."
    ),
    source="Michelle, VWAP_Bounce_Break_Formatted.docx, plus approved operator clarifications.",
    execution_limitations=(
        "Live mode enters from a trade event during the second developing green candle.",
        "Historical mode requires the second completed green candle to close at/above the first "
        "confirmation close and fills at the next eligible M1 open.",
        "Previous-day and premarket levels remain supplementary metadata and are not filters.",
    ),
    reason_codes=(
        ReasonCode.SIGNAL,
        ReasonCode.NO_SIGNAL,
        ReasonCode.INSUFFICIENT_DATA,
        ReasonCode.STALE_DATA,
        ReasonCode.NOT_ELIGIBLE,
    ),
)


def _hammer(bar: Candle, vwap: Decimal) -> bool:
    if bar.close >= vwap or bar.high <= bar.low:
        return False
    body = abs(bar.close - bar.open)
    lower_wick = min(bar.open, bar.close) - bar.low
    return lower_wick >= Decimal(2) * body and (
        (bar.close - bar.low) / (bar.high - bar.low) >= Decimal("0.60")
    )


def _base(bars: Sequence[Candle], vwaps: Sequence[Decimal | None], index: int) -> bool:
    if index < 2:
        return False
    group = bars[index - 2 : index + 1]
    group_vwaps = vwaps[index - 2 : index + 1]
    if any(vwap is None or bar.close >= vwap for bar, vwap in zip(group, group_vwaps, strict=True)):
        return False
    combined = max(bar.high for bar in group) - min(bar.low for bar in group)
    return combined <= Decimal("0.0015") * group[-1].close


def _engulfing(bars: Sequence[Candle], vwaps: Sequence[Decimal | None], index: int) -> bool:
    if index < 1 or vwaps[index] is None or vwaps[index - 1] is None:
        return False
    prior, current = bars[index - 1], bars[index]
    return (
        prior.close < prior.open
        and current.close > current.open
        and prior.close < vwaps[index - 1]
        and current.close < vwaps[index]
        and current.open <= prior.close
        and current.close >= prior.open
    )


def _scan_cycle(
    bars: Sequence[Candle], consumed_after: datetime | None
) -> tuple[dict[str, Any] | None, tuple[Candle, Decimal] | None]:
    """Causally reconstruct the newest unconsumed cycle and replay entry."""
    vwaps = session_vwaps(bars)
    ema9 = ema_values(bars, 9)
    ema20 = ema_values(bars, 20)
    cycle: dict[str, Any] | None = None
    replay_entry: tuple[Candle, Decimal] | None = None
    for index, bar in enumerate(bars):
        if consumed_after is not None and bar.start <= consumed_after:
            continue
        vwap, fast, slow = vwaps[index], ema9[index], ema20[index]
        if vwap is None or fast is None or slow is None:
            continue
        if (
            cycle is not None
            and cycle.get("first_time") is None
            and (bar.start - cycle["breakdown"] > timedelta(minutes=30))
        ):
            cycle = None
        bearish = bar.close < vwap and bar.close < fast and bar.close < slow and fast < slow < vwap
        if cycle is None:
            if bearish:
                cycle = {
                    "id": f"{bar.start.isoformat()}:vwap-cycle",
                    "breakdown": bar.start,
                    "low": bar.low,
                    "low_vwap": vwap,
                    "bottom": None,
                    "first_time": None,
                    "first_close": None,
                }
            else:
                continue

        if bar.low < cycle["low"]:
            cycle["low"] = bar.low
            cycle["low_vwap"] = vwap
        if cycle["low_vwap"] - cycle["low"] > Decimal("2.00"):
            cycle = None
            continue
        if cycle["bottom"] is None:
            if _hammer(bar, vwap):
                cycle["bottom"] = "hammer"
            elif _base(bars, vwaps, index):
                cycle["bottom"] = "base"
            elif _engulfing(bars, vwaps, index):
                cycle["bottom"] = "bullish_engulfing"

        if cycle["bottom"] is None:
            continue
        if cycle["first_time"] is None:
            if index < 3 or ema9[index - 3] is None or ema20[index - 3] is None:
                continue
            recovery = fast > ema9[index - 3] and abs(fast - slow) < abs(
                ema9[index - 3] - ema20[index - 3]
            )
            crossed = bar.close > bar.open and bar.low <= vwap and bar.close > vwap and recovery
            if crossed and bar.start - cycle["breakdown"] <= timedelta(minutes=30):
                cycle["first_time"] = bar.start
                cycle["first_close"] = bar.close
            continue

        if bar.start <= cycle["first_time"]:
            continue
        if (
            bar.close > bar.open
            and bar.close > vwap
            and bar.close >= cycle["first_close"]
            and bar.close <= vwap + Decimal("0.50")
        ):
            replay_entry = (bar, vwap)
    return cycle, replay_entry


class VwapBounceBreakMichelleBuy(BuyStrategy):
    """Two-green-candle VWAP recovery with persistent cycle consumption."""

    spec = _SPEC
    EVIDENCE_INTERFACE_VERSION = EVIDENCE_INTERFACE_VERSION

    def build_evidence(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
        evaluation: StrategyEvaluation,
    ) -> DecisionEvidence:
        """The VWAP breakdown/recovery cycle that produced this entry."""
        self.spec.parameter_schema.validate(params)
        bars = regular_session_bars(context.candles.get(CandleTimeframe.M1, ()), context.session_id)
        vwaps = session_vwaps(bars)
        ema9, ema20 = ema_values(bars, 9), ema_values(bars, 20)
        reference = safe_decimal(state.get("entry_reference_price"))
        entry_vwap = safe_decimal(state.get("entry_vwap"))
        evidence: dict[str, object] = {
            "cycle": {
                "cycle_id": state.get("signal_cycle_id"),
                "breakdown_time": state.get("breakdown_time"),
                "cycle_low": safe_decimal(state.get("cycle_low")),
                "vwap_at_cycle_low": safe_decimal(state.get("cycle_low_vwap")),
                "bottom_pattern": state.get("bottom_pattern"),
                "first_confirmation_time": state.get("first_confirmation_time"),
                "first_confirmation_close": safe_decimal(state.get("first_confirmation_close")),
            },
            "trigger": {
                "kind": "live_trade_second_green" if live_trade(context) is not None
                else "completed_second_green_candle",
                "event_time": state.get("signal_event_time"),
                "reference_price": reference,
                "vwap_at_entry": entry_vwap,
                "distance_above_vwap": None
                if reference is None or entry_vwap is None else reference - entry_vwap,
                "max_distance_above_vwap": Decimal("0.50"),
            },
            "latest_completed_bar": None,
        }
        if bars:
            evidence["latest_completed_bar"] = {
                "start": bars[-1].start,
                "close": bars[-1].close,
                "vwap": vwaps[-1],
                "ema9": ema9[-1],
                "ema20": ema20[-1],
            }
        return make_evidence(
            self.spec, evaluation, context,
            reason_code="VWAP_RECOVERY_SECOND_GREEN",
            reason=(
                "a qualifying below-VWAP bottom was followed by a first confirmation candle "
                "above VWAP and a later green candle at or above its close"
            ),
            evidence=evidence,
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
        if state.get("eligibility") != ELIGIBILITY_WATCHING_BUY:
            return StrategyEvaluation.no_signal(self.spec, symbol, (ReasonCode.NOT_ELIGIBLE,))
        bars = regular_session_bars(context.candles.get(CandleTimeframe.M1, ()), context.session_id)
        if len(bars) < 20:
            return StrategyEvaluation.no_signal(self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,))
        session_key = context.session_id or bars[-1].start.astimezone(NEW_YORK).date().isoformat()
        consumed = (
            state_time(state.get("consumed_after"))
            if state.get("session_id") == session_key
            else None
        )
        cycle, replay_entry = _scan_cycle(bars, consumed)
        updates: dict[str, object] = {"session_id": session_key}
        if cycle is not None:
            updates.update(
                {
                    "cycle_id": cycle["id"],
                    "breakdown_time": cycle["breakdown"].isoformat(),
                    "cycle_low": str(cycle["low"]),
                    "cycle_low_vwap": str(cycle["low_vwap"]),
                    "bottom_pattern": cycle["bottom"] or "",
                    "first_confirmation_time": (
                        "" if cycle["first_time"] is None else cycle["first_time"].isoformat()
                    ),
                    "first_confirmation_close": (
                        "" if cycle["first_close"] is None else str(cycle["first_close"])
                    ),
                }
            )

        live = live_trade(context)
        if live is not None:
            price, event_time, developing = live
            local_event = event_time.astimezone(NEW_YORK)
            if (
                cycle is None
                or cycle["first_time"] is None
                or developing.start <= cycle["first_time"]
            ):
                updates["last_signal_event"] = state.get("last_signal_event", "")
                return StrategyEvaluation.no_signal(
                    self.spec, symbol, (ReasonCode.NO_SIGNAL,), state_updates=updates
                )
            latest_vwap = current_session_vwap(context, bars)
            if latest_vwap is None:
                return StrategyEvaluation.no_signal(
                    self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,), state_updates=updates
                )
            qualifies = (
                price > developing.open
                and price > latest_vwap
                and price >= cycle["first_close"]
                and price <= latest_vwap + Decimal("0.50")
            )
            reference_price = price
            entry_vwap = latest_vwap
            bar_time = event_time
        else:
            if replay_entry is None:
                return StrategyEvaluation.no_signal(
                    self.spec,
                    symbol,
                    (ReasonCode.NO_SIGNAL,),
                    bar_time=bars[-1].start,
                    state_updates=updates,
                )
            entry_bar, entry_vwap = replay_entry
            if entry_bar is not bars[-1]:
                return StrategyEvaluation.no_signal(
                    self.spec,
                    symbol,
                    (ReasonCode.NO_SIGNAL,),
                    bar_time=bars[-1].start,
                    state_updates=updates,
                )
            event_time = entry_bar.end
            local_event = event_time.astimezone(NEW_YORK)
            reference_price = entry_bar.close
            bar_time = entry_bar.start
            qualifies = True

        if not time(9, 30) <= local_event.time() < time(11, 30):
            return StrategyEvaluation.no_signal(
                self.spec,
                symbol,
                (ReasonCode.NOT_ELIGIBLE,),
                bar_time=bar_time,
                state_updates=updates,
            )
        if not qualifies:
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
        assert cycle is not None
        updates.update(
            {
                "last_signal_event": event_key,
                "signal_cycle_id": cycle["id"],
                "entry_vwap": str(entry_vwap),
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
