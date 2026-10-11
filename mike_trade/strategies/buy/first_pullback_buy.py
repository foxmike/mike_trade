"""First shallow price/volume pullback after a morning upward impulse."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
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
from mike_trade.strategies.buy._session import current_session_bars, latest_session_bars
from mike_trade.strategies.closed_bar import (
    REASON_CODES,
    STATE_LAST_SIGNAL_BAR,
    blocked,
    finish,
    gate,
    insufficient,
)
from mike_trade.strategies.indicators.vwap_bands import session_key

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "first_pullback_buy"
STATE_SETUP = "setup_state"
ONE = Decimal(1)
HUNDRED = Decimal(100)


class FirstPullbackBuy(BuyStrategy):
    """Consume one impulse and its first pullback; never rearm in the same session."""

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version="1.0.0",
        side=StrategyRole.BUY,
        category="technical",
        sub_category="intraday_session",
        reference_symbols=(),
        required_timeframes=(CandleTimeframe.M1,),
        warmup_bars=5,
        completed_bars_only=True,
        requires_volume=True,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=CandleTimeframe.M1,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec(
                    "impulse_bars",
                    ParameterKind.INT,
                    3,
                    3,
                    10,
                    description="Consecutive rising impulse bars. Default 3.",
                ),
                ParameterSpec(
                    "impulse_pct",
                    ParameterKind.DECIMAL,
                    Decimal(1),
                    Decimal("0.1"),
                    Decimal(10),
                    description="Minimum impulse gain from first open, percent. Default 1.",
                ),
                ParameterSpec(
                    "max_pullback_bars",
                    ParameterKind.INT,
                    5,
                    1,
                    15,
                    description="Maximum corrective bars before confirmation. Default 5.",
                ),
                ParameterSpec(
                    "max_retrace",
                    ParameterKind.DECIMAL,
                    Decimal("0.5"),
                    Decimal("0.1"),
                    Decimal("0.9"),
                    description="Maximum fraction of impulse range retraced. Default 0.5.",
                ),
                ParameterSpec(
                    "volume_multiple",
                    ParameterKind.DECIMAL,
                    Decimal("1.2"),
                    Decimal(1),
                    Decimal(5),
                    description="Impulse and rebound volume multiple. Default 1.2.",
                ),
                ParameterSpec(
                    "entry_end_minute",
                    ParameterKind.INT,
                    90,
                    15,
                    180,
                    description="Exclusive minutes after 09:30 ET for entry. Default 90.",
                ),
            )
        ),
        persistent_state_keys=(STATE_SETUP, STATE_LAST_SIGNAL_BAR),
        methodology=(
            "Identify the first rising price/volume impulse in the morning, consume only "
            "its first shallow lower-close pullback with declining volume, and BUY on the "
            "first completed close above the pullback highs with renewed volume."
        ),
        source=(
            "Concept: a strong-volume advance, an orderly lower-volume first pullback, then entry "
            "on the first candle making a new high (Warrior Trading, Pull Back Day Trading "
            "Strategies: https://www.warriortrading.com/pull-back-trading-strategy/; the page "
            "could not be fetched during review, so treat the citation as unverified). "
            "Numeric percentages, volume multiples, time cutoff and completed-close "
            "confirmation are engineering choices."
        ),
        execution_limitations=(
            "Only the first qualifying impulse and its first pullback are considered per session.",
            "A deep, long, or high-volume first pullback invalidates the setup for that session.",
            "Needs a complete-from-open prefix or a valid persisted setup snapshot; "
            "no orders or stops.",
        ),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        bars = cls.spec.parameter_schema.validate(params)["impulse_bars"]
        assert isinstance(bars, int)
        return bars + 2

    def evaluate(
        self, context: MarketContext, params: Mapping[str, object], state: Mapping[str, object]
    ) -> StrategyEvaluation:
        values = self.spec.parameter_schema.validate(params)
        impulse_bars = values["impulse_bars"]
        impulse_pct = values["impulse_pct"]
        max_pullback = values["max_pullback_bars"]
        max_retrace = values["max_retrace"]
        volume_multiple = values["volume_multiple"]
        end_minute = values["entry_end_minute"]
        assert isinstance(impulse_bars, int) and isinstance(max_pullback, int)
        assert isinstance(end_minute, int)
        assert isinstance(impulse_pct, Decimal) and isinstance(max_retrace, Decimal)
        assert isinstance(volume_multiple, Decimal)
        reason = gate(self.spec, context, state)
        found = latest_session_bars(context, 1)
        if found is None:
            return insufficient(self.spec, context)
        session, tail = found
        latest = tail[-1]
        session_id = session_key(latest)
        supplied = context.candles.get(CandleTimeframe.M1, ())
        signature = "|".join(
            str(values[key])
            for key in (
                "impulse_bars",
                "impulse_pct",
                "max_pullback_bars",
                "max_retrace",
                "volume_multiple",
                "entry_end_minute",
            )
        )
        saved = state.get(STATE_SETUP) if isinstance(state, Mapping) else None
        valid = (
            isinstance(saved, Mapping)
            and saved.get("version") == 1
            and saved.get("session") == session_id
            and saved.get("signature") == signature
        )
        if valid:
            try:
                last_end = datetime.fromisoformat(str(saved["last_end"]))
                phase = str(saved["phase"])
                if phase not in {"SEEK", "ADVANCE", "PULLBACK", "DONE"}:
                    valid = False
                high = Decimal(str(saved["impulse_high"]))
                low = Decimal(str(saved["impulse_low"]))
                impulse_volume = Decimal(str(saved["impulse_volume"]))
                pullback_low = Decimal(str(saved["pullback_low"]))
                pullback_high = Decimal(str(saved["pullback_high"]))
                pullback_volume = Decimal(str(saved["pullback_volume"]))
                pullback_count = int(saved["pullback_count"])
                previous_close = Decimal(str(saved["previous_close"]))
                if (
                    not session.open_utc < last_end <= latest.end
                    or pullback_count < 0
                    or any(
                        not number.is_finite() or number < 0
                        for number in (
                            high,
                            low,
                            impulse_volume,
                            pullback_low,
                            pullback_high,
                            pullback_volume,
                            previous_close,
                        )
                    )
                    or (phase in {"ADVANCE", "PULLBACK"} and (high <= low or impulse_volume <= 0))
                    or (phase == "PULLBACK" and (pullback_count < 1 or pullback_volume <= 0))
                ):
                    valid = False
            except (KeyError, TypeError, ValueError, InvalidOperation):
                valid = False
        if valid:
            if last_end == latest.end:
                if reason is not None:
                    return blocked(self.spec, context, reason)
                return finish(self.spec, context, latest, state, False)
            pending = tuple(bar for bar in supplied if last_end <= bar.start <= latest.start)
            if not pending or pending[0].start != last_end:
                return insufficient(self.spec, context)
            if any(a.end != b.start for a, b in zip(pending, pending[1:], strict=False)):
                return insufficient(self.spec, context)
        else:
            complete = current_session_bars(context)
            if complete is None:
                return insufficient(self.spec, context)
            pending = complete[1]
            phase = "SEEK"
            high = low = impulse_volume = Decimal(0)
            pullback_low = pullback_high = pullback_volume = Decimal(0)
            pullback_count = 0
            previous_close = Decimal(0)

        triggered = False
        for bar in pending:
            if bar.volume <= 0 or session_key(bar) != session_id:
                return insufficient(self.spec, context)
            if bar.start >= session.open_utc + timedelta(minutes=end_minute):
                phase = "DONE"
            elif phase == "SEEK":
                prefix = tuple(item for item in supplied if item.end <= bar.end)
                group = prefix[-impulse_bars:]
                if (
                    len(group) == impulse_bars
                    and all(item.start >= session.open_utc for item in group)
                    and all(a.end == b.start for a, b in zip(group, group[1:], strict=False))
                    and all(
                        b.close > a.close and b.high > a.high and b.low > a.low
                        for a, b in zip(group, group[1:], strict=False)
                    )
                    and bar.close >= group[0].open * (ONE + impulse_pct / HUNDRED)
                    and bar.volume
                    >= volume_multiple
                    * (
                        sum((item.volume for item in group[:-1]), Decimal(0))
                        / Decimal(impulse_bars - 1)
                    )
                ):
                    phase = "ADVANCE"
                    high = max(item.high for item in group)
                    low = min(item.low for item in group)
                    impulse_volume = sum((item.volume for item in group), Decimal(0)) / Decimal(
                        impulse_bars
                    )
            elif phase == "ADVANCE":
                if bar.close < previous_close:
                    phase = "PULLBACK"
                    pullback_low, pullback_high = bar.low, bar.high
                    pullback_volume, pullback_count = bar.volume, 1
                    if (
                        bar.low <= low
                        or high <= low
                        or (high - bar.low) / (high - low) > max_retrace
                        or bar.volume >= impulse_volume
                    ):
                        phase = "DONE"
                else:
                    high = max(high, bar.high)
                    if bar.low <= low:
                        phase = "DONE"
            elif phase == "PULLBACK":
                new_low = min(pullback_low, bar.low)
                if (
                    new_low <= low
                    or (high - new_low) / (high - low) > max_retrace
                    or pullback_count > max_pullback
                ):
                    phase = "DONE"
                elif bar.close > pullback_high and bar.volume >= volume_multiple * (
                    pullback_volume / Decimal(pullback_count)
                ):
                    triggered = bar is latest
                    phase = "DONE"
                else:
                    pullback_count += 1
                    pullback_low = new_low
                    pullback_high = max(pullback_high, bar.high)
                    pullback_volume += bar.volume
                    if pullback_count > max_pullback or bar.volume >= impulse_volume:
                        phase = "DONE"
            previous_close = bar.close

        updates = {
            STATE_SETUP: {
                "version": 1,
                "session": session_id,
                "signature": signature,
                "last_end": latest.end.isoformat(),
                "phase": phase,
                "impulse_high": str(high),
                "impulse_low": str(low),
                "impulse_volume": str(impulse_volume),
                "pullback_low": str(pullback_low),
                "pullback_high": str(pullback_high),
                "pullback_volume": str(pullback_volume),
                "pullback_count": pullback_count,
                "previous_close": str(previous_close),
            }
        }
        if reason is not None:
            return blocked(self.spec, context, reason, updates)
        return finish(self.spec, context, latest, state, triggered, updates)
