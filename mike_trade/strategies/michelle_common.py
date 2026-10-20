"""Pure shared calculations for Michelle's event-driven intraday strategies."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from mike_trade.market_data.candles import Candle, CandleStatus, CandleTimeframe

NEW_YORK = ZoneInfo("America/New_York")
ZERO = Decimal("0")

MICHELLE_BUY_IDS = frozenset({"five_minute_orb_michelle_buy", "vwap_bounce_break_michelle_buy"})

#: Michelle's corrected VWAP Bounce/Break (business rules 1-55).  Independent of the original
#: strategies: deliberately NOT in ``MICHELLE_BUY_IDS`` because the original strategies' shared
#: attempt limiter (2 per 10 minutes, never adjacent minutes) is not part of the corrected rules.
CORRECTED_BUY_ID = "vwap_bounce_break_michelle_corrected_buy"
CORRECTED_SELL_ID = "vwap_bounce_break_michelle_corrected_sell"
CORRECTED_BUY_IDS = frozenset({CORRECTED_BUY_ID})
CORRECTED_SELL_IDS = frozenset({CORRECTED_SELL_ID})
#: Every BUY whose persisted state carries position/fill facts for its protective SELL.
FACT_BUY_IDS = MICHELLE_BUY_IDS | CORRECTED_BUY_IDS


def regular_session_bars(candles: Iterable[Candle], session_id: str | None = None) -> list[Candle]:
    """Return ascending completed M1 bars from one regular-session date."""
    bars = [
        bar
        for bar in candles
        if isinstance(bar, Candle)
        and bar.timeframe is CandleTimeframe.M1
        and bar.status is CandleStatus.CLOSED
    ]
    if not bars:
        return []
    target = session_id or bars[-1].start.astimezone(NEW_YORK).date().isoformat()
    if target.startswith("XNYS-"):
        target = target.removeprefix("XNYS-")
    return [bar for bar in bars if bar.start.astimezone(NEW_YORK).date().isoformat() == target]


def adjacent(bars: Sequence[Candle]) -> bool:
    return all(cur.start == prior.end for prior, cur in zip(bars, bars[1:], strict=False))


def session_vwaps(bars: Sequence[Candle]) -> list[Decimal | None]:
    """Completed-bar regular-session VWAP using typical price and volume."""
    weighted = ZERO
    volume = ZERO
    values: list[Decimal | None] = []
    for bar in bars:
        if bar.volume > ZERO:
            typical = (bar.high + bar.low + bar.close) / Decimal(3)
            weighted += typical * bar.volume
            volume += bar.volume
        values.append(None if volume <= ZERO else weighted / volume)
    return values


def current_session_vwap(context: object, bars: Sequence[Candle]) -> Decimal | None:
    """Point-in-time VWAP, including the developing trade-built M1 bar when present."""
    developing = getattr(context, "developing_candles", {}) or {}
    current = developing.get(CandleTimeframe.M1)
    source = [*bars, current] if isinstance(current, Candle) else list(bars)
    values = session_vwaps(source)
    return values[-1] if values else None


def ema_values(bars: Sequence[Candle], period: int) -> list[Decimal | None]:
    """SMA-seeded EMA values over completed closes."""
    values: list[Decimal | None] = [None] * len(bars)
    if len(bars) < period:
        return values
    ema = sum((bar.close for bar in bars[:period]), ZERO) / Decimal(period)
    values[period - 1] = ema
    alpha = Decimal(2) / Decimal(period + 1)
    for index in range(period, len(bars)):
        ema = alpha * bars[index].close + (Decimal(1) - alpha) * ema
        values[index] = ema
    return values


def daily_wilder_atr(candles: Iterable[Candle], period: int = 14) -> Decimal | None:
    """Wilder seed ATR from the preceding ``period`` daily true ranges.

    The approved contract supplies the preceding 14 completed trading days plus the
    earlier close needed by the first true range, hence 15 D1 bars are required.
    """
    bars = [
        bar
        for bar in candles
        if isinstance(bar, Candle)
        and bar.timeframe is CandleTimeframe.D1
        and bar.status is CandleStatus.CLOSED
    ]
    if len(bars) < period + 1:
        return None
    bars = bars[-(period + 1) :]
    ranges = [
        max(
            bar.high - bar.low,
            abs(bar.high - previous.close),
            abs(bar.low - previous.close),
        )
        for previous, bar in zip(bars[:-1], bars[1:], strict=True)
    ]
    return sum(ranges, ZERO) / Decimal(period)


def state_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def minute_key(value: datetime) -> str:
    return value.astimezone(NEW_YORK).replace(second=0, microsecond=0).isoformat()


def live_trade(context: object) -> tuple[Decimal, datetime, Candle] | None:
    """Return current trade and developing M1 bar when this is a live-event context."""
    trade = getattr(context, "latest_trade", None)
    developing = getattr(context, "developing_candles", {}) or {}
    bar = developing.get(CandleTimeframe.M1)
    if trade is None or not isinstance(bar, Candle) or bar.status is not CandleStatus.DEVELOPING:
        return None
    return trade.price, trade.provider_ts.astimezone(UTC), bar


def entry_attempt_allowed(attempts: Iterable[datetime], candidate: datetime) -> bool:
    """Shared Michelle gate: at most two in 10 minutes and never adjacent M1 candles."""
    at = candidate.astimezone(UTC)
    recent = sorted(
        item.astimezone(UTC)
        for item in attempts
        if timedelta(0) <= at - item.astimezone(UTC) < timedelta(minutes=10)
    )
    if len(recent) >= 2:
        return False
    candidate_minute = candidate.astimezone(NEW_YORK).replace(second=0, microsecond=0)
    return not recent or candidate_minute - recent[-1].astimezone(NEW_YORK).replace(
        second=0, microsecond=0
    ) != timedelta(minutes=1)


def normalized_attempts(attempts: Iterable[datetime], now: datetime) -> tuple[datetime, ...]:
    at = now.astimezone(UTC)
    return tuple(
        sorted(
            item.astimezone(UTC)
            for item in attempts
            if timedelta(0) <= at - item.astimezone(UTC) < timedelta(minutes=10)
        )
    )


def decode_attempts(value: object) -> tuple[datetime, ...]:
    if not isinstance(value, list | tuple):
        return ()
    parsed = [state_time(item) for item in value]
    return tuple(item.astimezone(UTC) for item in parsed if item is not None)


def encode_attempts(value: Iterable[datetime]) -> tuple[str, ...]:
    return tuple(item.astimezone(UTC).isoformat() for item in value)


def safe_decimal(value: object) -> Decimal | None:
    if isinstance(value, Decimal) and value.is_finite():
        return value
    if isinstance(value, str):
        try:
            parsed = Decimal(value)
        except Exception:  # invalid persisted state fails closed
            return None
        return parsed if parsed.is_finite() else None
    return None


def signal_facts(state: Mapping[str, object]) -> dict[str, object]:
    """Copy the approved fill/protection facts emitted by a Michelle BUY."""
    keys = (
        "signal_cycle_id",
        "entry_vwap",
        "entry_orb_high",
        "entry_reference_price",
        "signal_event_time",
        "position_cycle_id",
        "position_fill_price",
        "position_fill_time",
        "position_vwap_at_entry",
    )
    return {key: state[key] for key in keys if key in state}


def corrected_signal_facts(state: Mapping[str, object]) -> dict[str, object]:
    """Copy the approved fill/protection/evidence facts emitted by the corrected BUY."""
    keys = (
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
    return {key: state[key] for key in keys if key in state}
