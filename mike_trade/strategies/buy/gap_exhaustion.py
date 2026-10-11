"""Downside exhaustion-gap BUY: a gap below the prior low at the end of a multi-session decline,
confirmed when the same session closes back above the gap."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
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
from mike_trade.strategies.buy._session import complete_session_reference, current_session_bars
from mike_trade.strategies.closed_bar import (
    REASON_CODES,
    STATE_LAST_SIGNAL_BAR,
    blocked,
    finish,
    gate,
    insufficient,
)

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "gap_exhaustion"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1
WARMUP_BARS = 2
HUNDRED = Decimal(100)
MINUTE_SECONDS = 60

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.BUY,
    category="technical",
    sub_category="gap_trading",
    reference_symbols=(),
    required_timeframes=(TIMEFRAME,),
    warmup_bars=WARMUP_BARS,
    completed_bars_only=True,
    requires_volume=True,
    trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
    trigger_timeframe=TIMEFRAME,
    parameter_schema=ParameterSchema(
        (
            ParameterSpec(
                "trend_sessions", ParameterKind.INT, 4, 2, 8,
                description=(
                    "Consecutive completed sessions that each closed lower than the one before. "
                    "Needs trend_sessions + 1 consecutive complete sessions. Default 4."
                ),
            ),
            ParameterSpec(
                "min_decline_pct", ParameterKind.DECIMAL, Decimal(3), Decimal("0.5"), Decimal(30),
                description=(
                    "Minimum close-to-close decline over the trend, percent of the first close. "
                    "Default 3."
                ),
            ),
            ParameterSpec(
                "min_gap_pct", ParameterKind.DECIMAL, Decimal(1), Decimal("0.1"), Decimal(20),
                description=(
                    "Minimum distance of the session open below the previous session's low, percent "
                    "of the previous close. Default 1."
                ),
            ),
            ParameterSpec(
                "min_relative_volume", ParameterKind.DECIMAL, Decimal("1.5"), Decimal(0), Decimal(10),
                description=(
                    "Minimum ratio of the current session's per-minute volume so far to the trend "
                    "sessions' per-minute volume; 0 disables the volume test. Default 1.5."
                ),
            ),
        )
    ),
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=(
        "BUY when, after trend_sessions consecutive lower session closes totalling at least "
        "min_decline_pct, the regular session opens at least min_gap_pct below the previous "
        "session's low (a gap), session volume so far is at least min_relative_volume times the "
        "trend sessions' per-minute average, and a completed M1 close is the first to climb back "
        "above that previous low (the gap is closed within the session)."
    ),
    source=(
        "Concept (general chart-pattern literature): the exhaustion gap - a gap late in an extended "
        "move, usually on heavy volume, that is soon closed and marks the end of the move "
        "(Robert Edwards and John Magee, Technical Analysis of Stock Trends; John J. Murphy, "
        "Technical Analysis of the Financial Markets). That literature recognises the pattern from "
        "the gap being closed; it gives no numeric thresholds. The session count, the decline and "
        "gap sizes, the relative-volume test and the 'closed within the session' confirmation are "
        "this library's engineering choices."
    ),
    execution_limitations=(
        "Entry signal only: no stops, sizing, targets, or orders.",
        "Needs the session reference history: trend_sessions + 1 consecutive, fully observed "
        "regular sessions (with volume) immediately before today and today's M1 bars from the "
        "09:30 open without a missing minute; any hole fails closed (INSUFFICIENT_DATA). The "
        "history is the M1 bars the tracker has observed (production warmup loads about 14 "
        "calendar days), so a thin feed that leaves sessions incomplete never signals.",
        "Distinct from gap_fill (reclaim of the session open after any gap down), gap_reversal "
        "(opening range and VWAP) and previous_day_low_reversal (single-bar sweep and reclaim): "
        "this rule additionally requires a multi-session decline, an opening gap beyond the "
        "prior low, abnormal participation and the first close back above the gap.",
        "Regular-session completed bars only; no extended-hours prices; one signal per session "
        "(the first close above the gap edge).",
    ),
    reason_codes=REASON_CODES,
)


class GapExhaustionBuy(BuyStrategy):
    """BUY the first completed close back above the previous low after an exhaustion gap down."""

    spec = _SPEC

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        values = self.spec.parameter_schema.validate(params)
        sessions, decline, gap, rel_volume = (
            values["trend_sessions"], values["min_decline_pct"], values["min_gap_pct"],
            values["min_relative_volume"],
        )
        assert isinstance(sessions, int) and isinstance(decline, Decimal)
        assert isinstance(gap, Decimal) and isinstance(rel_volume, Decimal)
        reason = gate(self.spec, context, state)
        if reason is not None:
            return blocked(self.spec, context, reason)
        reference = complete_session_reference(context)
        found = current_session_bars(context)
        if reference is None or found is None or len(found[1]) < 2:
            return insufficient(self.spec, context)
        trend = reference.history[-(sessions + 1):]
        if len(trend) < sessions + 1:
            return insufficient(self.spec, context)
        _, bars = found
        previous = reference.previous
        assert previous is not None and reference.current_open is not None
        latest, edge = bars[-1], previous.low

        # 1. a decline: every one of the last `sessions` session-to-session closes is lower
        declining = all(b.close < a.close for a, b in zip(trend, trend[1:], strict=False))
        drop_pct = (trend[0].close - trend[-1].close) / trend[0].close * HUNDRED
        # 2. an opening gap below the previous session's low (not merely below its close); a
        #    positive gap percentage already means open < previous low
        gap_pct = (edge - reference.current_open) / previous.close * HUNDRED
        # 3. participation: per-minute volume of today so far against the trend sessions
        volume_ok = True
        if rel_volume > 0:
            if any(item.volume is None for item in trend):
                return insufficient(self.spec, context)
            minutes = sum(
                (item.close_time - item.open_time).total_seconds() / MINUTE_SECONDS for item in trend
            )
            baseline = sum((item.volume for item in trend if item.volume is not None), Decimal(0)) / Decimal(
                str(minutes)
            )
            if baseline <= 0:
                return insufficient(self.spec, context)
            today = sum((b.volume for b in bars), Decimal(0)) / Decimal(len(bars))
            volume_ok = today / baseline >= rel_volume
        # 4. confirmation: the first completed close above the gap edge in this session
        first_closure = latest.close > edge and all(b.close <= edge for b in bars[:-1])

        triggered = (
            declining and drop_pct >= decline and gap_pct >= gap and volume_ok and first_closure
        )
        return finish(self.spec, context, latest, state, triggered)
