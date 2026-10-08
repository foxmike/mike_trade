"""Pure spread and liquidity entry filters over a supplied quote snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Protocol

__all__ = ["SpreadFilter", "LiquidityFilter", "MarketFilterDecision", "MarketFilterReason"]

_ZERO = Decimal("0")
_TEN_THOUSAND = Decimal("10000")


class MarketFilterReason(str, Enum):
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    INSUFFICIENT_LIQUIDITY = "INSUFFICIENT_LIQUIDITY"
    QUOTE_MISSING = "QUOTE_MISSING"
    QUOTE_STALE = "QUOTE_STALE"
    QUOTE_CROSSED = "QUOTE_CROSSED"
    QUOTE_LOCKED = "QUOTE_LOCKED"
    UNKNOWN_PROVENANCE = "UNKNOWN_PROVENANCE"
    INVALID_QUOTE = "INVALID_QUOTE"


class QuoteSnapshot(Protocol):
    bid: Decimal
    ask: Decimal
    bid_size: Decimal | None
    ask_size: Decimal | None
    receive_ts: datetime
    feed_id: str | None


@dataclass(frozen=True, slots=True)
class SpreadFilter:
    max_absolute: Decimal | None = None
    max_basis_points: Decimal | None = None

    def __post_init__(self) -> None:
        if self.max_absolute is None and self.max_basis_points is None:
            raise ValueError("at least one spread maximum is required")
        _optional_nonnegative(self.max_absolute, "max_absolute")
        _optional_nonnegative(self.max_basis_points, "max_basis_points")


@dataclass(frozen=True, slots=True)
class LiquidityFilter:
    minimum_recent_volume: Decimal | None = None
    minimum_quote_size: Decimal | None = None
    max_quote_age: timedelta = timedelta(seconds=5)

    def __post_init__(self) -> None:
        if self.minimum_recent_volume is None and self.minimum_quote_size is None:
            raise ValueError("at least one liquidity minimum is required")
        _optional_nonnegative(self.minimum_recent_volume, "minimum_recent_volume")
        _optional_nonnegative(self.minimum_quote_size, "minimum_quote_size")
        if not isinstance(self.max_quote_age, timedelta) or self.max_quote_age < timedelta(0):
            raise ValueError("max_quote_age must be a non-negative timedelta")


@dataclass(frozen=True, slots=True)
class MarketFilterDecision:
    allowed: bool
    reasons: tuple[MarketFilterReason, ...]
    evaluated_at: datetime
    feed_provenance: str | None
    spread_absolute: Decimal | None = None
    spread_basis_points: Decimal | None = None

    @property
    def blocked(self) -> bool:
        return not self.allowed

    @classmethod
    def evaluate(
        cls, *, now: datetime, quote: QuoteSnapshot | None,
        spread_filter: SpreadFilter | None = None,
        liquidity_filter: LiquidityFilter | None = None,
        recent_volume: Decimal | None = None,
    ) -> "MarketFilterDecision":
        return _evaluate_market_filters(
            now=now, quote=quote, spread_filter=spread_filter,
            liquidity_filter=liquidity_filter, recent_volume=recent_volume,
        )


def _evaluate_market_filters(
    *, now: datetime, quote: QuoteSnapshot | None, spread_filter: SpreadFilter | None = None,
    liquidity_filter: LiquidityFilter | None = None, recent_volume: Decimal | None = None,
) -> MarketFilterDecision:
    """Evaluate supplied data only; this function never fetches or persists anything."""
    instant = _utc(now, "now")
    if spread_filter is not None and not isinstance(spread_filter, SpreadFilter):
        raise TypeError("spread_filter must be SpreadFilter or None")
    if liquidity_filter is not None and not isinstance(liquidity_filter, LiquidityFilter):
        raise TypeError("liquidity_filter must be LiquidityFilter or None")
    if recent_volume is not None:
        _nonnegative(recent_volume, "recent_volume")
    reasons: list[MarketFilterReason] = []
    snapshot_stale = False
    if quote is not None:
        freshness = getattr(quote, "freshness", None)
        snapshot_stale = bool(getattr(freshness, "stale", False))
        wrapped_quote = getattr(quote, "latest_quote", None)
        if wrapped_quote is not None:
            quote = wrapped_quote
    provenance = None if quote is None else getattr(quote, "feed_id", None)
    if quote is None:
        reasons.append(MarketFilterReason.QUOTE_MISSING)
    else:
        if (
            not isinstance(provenance, str) or not provenance.strip()
            or provenance.strip().casefold() in {"unknown", "none", "unavailable"}
        ):
            reasons.append(MarketFilterReason.UNKNOWN_PROVENANCE)
            provenance = None
        else:
            provenance = provenance.strip()
        bid, ask = getattr(quote, "bid", None), getattr(quote, "ask", None)
        receive_ts = getattr(quote, "receive_ts", None)
        stale = snapshot_stale or getattr(quote, "stale", False)
        if not isinstance(bid, Decimal) or not isinstance(ask, Decimal) or not bid.is_finite() or not ask.is_finite():
            reasons.append(MarketFilterReason.INVALID_QUOTE)
        elif bid < _ZERO or ask < _ZERO:
            reasons.append(MarketFilterReason.INVALID_QUOTE)
        elif bid > ask:
            reasons.append(MarketFilterReason.QUOTE_CROSSED)
        elif bid == ask:
            reasons.append(MarketFilterReason.QUOTE_LOCKED)
        if not isinstance(receive_ts, datetime) or receive_ts.tzinfo is None or receive_ts.utcoffset() is None:
            reasons.append(MarketFilterReason.QUOTE_STALE)
        elif receive_ts.astimezone(timezone.utc) > instant or stale:
            reasons.append(MarketFilterReason.QUOTE_STALE)
        elif instant - receive_ts.astimezone(timezone.utc) > (
            liquidity_filter.max_quote_age
            if liquidity_filter is not None else timedelta(seconds=5)
        ):
            reasons.append(MarketFilterReason.QUOTE_STALE)
        spread_abs = spread_bps = None
        if isinstance(bid, Decimal) and isinstance(ask, Decimal) and bid.is_finite() and ask.is_finite() and ask > bid and bid >= _ZERO:
            spread_abs = ask - bid
            midpoint = (ask + bid) / Decimal("2")
            if midpoint > _ZERO:
                spread_bps = spread_abs / midpoint * _TEN_THOUSAND
            if spread_filter is not None:
                if spread_filter.max_absolute is not None and spread_abs > spread_filter.max_absolute:
                    reasons.append(MarketFilterReason.SPREAD_TOO_WIDE)
                if spread_filter.max_basis_points is not None and (
                    spread_bps is None or spread_bps > spread_filter.max_basis_points
                ):
                    reasons.append(MarketFilterReason.SPREAD_TOO_WIDE)
        if liquidity_filter is not None:
            if liquidity_filter.minimum_recent_volume is not None and (
                recent_volume is None or recent_volume < liquidity_filter.minimum_recent_volume
            ):
                reasons.append(MarketFilterReason.INSUFFICIENT_LIQUIDITY)
            if liquidity_filter.minimum_quote_size is not None:
                bid_size, ask_size = getattr(quote, "bid_size", None), getattr(quote, "ask_size", None)
                if (
                    not isinstance(bid_size, Decimal) or not isinstance(ask_size, Decimal)
                    or bid_size < liquidity_filter.minimum_quote_size
                    or ask_size < liquidity_filter.minimum_quote_size
                ):
                    reasons.append(MarketFilterReason.INSUFFICIENT_LIQUIDITY)
    unique_reasons = tuple(dict.fromkeys(reasons))
    return MarketFilterDecision(
        allowed=not unique_reasons, reasons=unique_reasons, evaluated_at=instant,
        feed_provenance=provenance,
        spread_absolute=spread_abs if quote is not None else None,
        spread_basis_points=spread_bps if quote is not None else None,
    )


def _nonnegative(value: object, name: str) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal")
    if not value.is_finite() or value < _ZERO:
        raise ValueError(f"{name} must be finite and non-negative")


def _optional_nonnegative(value: Decimal | None, name: str) -> None:
    if value is not None:
        _nonnegative(value, name)


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must use UTC")
    return value.astimezone(timezone.utc)
