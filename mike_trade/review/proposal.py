"""Trade proposals and the compact, point-in-time request a reviewer receives.

A :class:`TradeProposal` is what the existing arbitration selected *before* any order exists: the
action (BUY or SELL), the symbol, the originating strategies with their structured evidence, and
(for a SELL) the position and its protective stop.  :func:`build_review_request` turns it plus the
already-assembled :class:`~mike_trade.market_data.context.MarketContext` into a small JSON
document.  The context is the same immutable, point-in-time snapshot the strategies evaluated, so
a reviewer can never see a candle that had not completed by the decision time; the builder also
verifies that explicitly and refuses (``ReviewLookAheadError``) otherwise.

The request carries no credentials, no account identifiers and no balances: only market data, the
proposal, the position that would be closed, and its stop.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from mike_trade.domain.errors import MikeTradeError
from mike_trade.market_data.candles import Candle, CandleTimeframe
from mike_trade.review.evidence import DecisionEvidence, to_json_value
from mike_trade.review.reviewer import REVIEW_PROTOCOL, canonical_json
from mike_trade.scheduling import calendar as exchange_calendar

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "REQUEST_SCHEMA_VERSION",
    "ReviewLookAheadError",
    "Contributor",
    "PositionView",
    "StopView",
    "StateToken",
    "TradeProposal",
    "build_review_request",
    "request_hash",
]

REQUEST_SCHEMA_VERSION = 1
_ZERO = Decimal(0)


class ReviewLookAheadError(MikeTradeError):
    """A request would have exposed data from after the decision time."""


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class Contributor:
    """One strategy that took part in the decision (signalled for this action on this tick)."""

    strategy_id: str
    strategy_version: str
    signal_id: str
    selected: bool
    #: ``None`` only for a contributor that was not selected for review and lacks evidence.
    evidence: DecisionEvidence | None = None


@dataclass(frozen=True, slots=True)
class PositionView:
    """The real position a SELL would close (fill-derived average entry, never a reference)."""

    cycle_id: str
    quantity: Decimal
    entry_price: Decimal
    entry_time: datetime
    direction: str = "LONG"


@dataclass(frozen=True, slots=True)
class StopView:
    """Existing protective-stop information (never modified by a review)."""

    kind: str
    initial_stop: Decimal | None = None
    current_stop: Decimal | None = None
    trail_percent: Decimal | None = None
    broker_held: bool | None = None


@dataclass(frozen=True, slots=True)
class StateToken:
    """Facts that must still hold for a delayed approval to authorize the proposal.

    ``facts`` are structural (symbol flat / cycle id / remaining quantity / exit or entry pending /
    protective-order status / entry gate open / end-of-day begun).  ``latest_bar_end`` is the end
    of the newest completed bar, used only for the freshness rule of
    :meth:`~mike_trade.review.stage.DecisionReviewStage.still_valid`.
    """

    facts: Mapping[str, Any]
    latest_bar_end: datetime | None = None

    def changed_fields(self, other: StateToken) -> tuple[str, ...]:
        keys = sorted(set(self.facts) | set(other.facts))
        return tuple(k for k in keys if self.facts.get(k) != other.facts.get(k))


@dataclass(frozen=True, slots=True)
class TradeProposal:
    """An arbitration-selected action awaiting review."""

    mode: str
    action: str
    symbol: str
    as_of: datetime
    selected_strategy_id: str
    selected_signal_id: str
    contributors: tuple[Contributor, ...]
    reference_price: Decimal | None = None
    position: PositionView | None = None
    stop: StopView | None = None
    #: Source of the exit being reviewed; only ``STRATEGY_SELL`` may ever be reviewed.
    exit_source: str | None = None
    state: StateToken | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if self.action not in ("BUY", "SELL"):
            raise ValueError(f"action must be BUY or SELL; got {self.action!r}")
        if self.mode not in ("BACKTEST", "SHADOW", "PAPER", "LIVE"):
            raise ValueError(f"mode must be BACKTEST, SHADOW, PAPER or LIVE; got {self.mode!r}")
        if self.as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        if not any(item.selected for item in self.contributors):
            raise ValueError("a proposal needs the selected strategy among its contributors")
        if self.action == "SELL" and self.position is None:
            raise ValueError("a SELL proposal needs the position it would close")

    @property
    def strategy_ids(self) -> tuple[str, ...]:
        return tuple(sorted({item.strategy_id for item in self.contributors}))

    @property
    def proposal_id(self) -> str:
        return f"{self.symbol}:{self.action}:{self.selected_signal_id}"


# ------------------------------------------------------------------ request construction


def _candle_row(candle: Candle, as_of: datetime) -> dict[str, Any]:
    if candle.end > as_of:
        raise ReviewLookAheadError(
            f"{candle.symbol} {candle.timeframe.value} candle ending {_iso(candle.end)} is after "
            f"the decision time {_iso(as_of)}"
        )
    return {
        "start": _iso(candle.start),
        "end": _iso(candle.end),
        "open": str(candle.open),
        "high": str(candle.high),
        "low": str(candle.low),
        "close": str(candle.close),
        "volume": str(candle.volume),
    }


def _last_price(context: MarketContext, as_of: datetime) -> tuple[Decimal, str] | None:
    trade = context.latest_trade
    if trade is not None and trade.provider_ts <= as_of:
        return trade.price, "last_trade"
    quote = context.latest_quote
    if quote is not None and quote.provider_ts <= as_of:
        return (quote.bid + quote.ask) / Decimal(2), "quote_mid"
    bars = context.candles.get(CandleTimeframe.M1, ())
    if bars:
        return bars[-1].close, "last_completed_1m_close"
    return None


def _session_clock(as_of: datetime) -> dict[str, Any]:
    local = as_of.astimezone(exchange_calendar.NEW_YORK)
    info: dict[str, Any] = {
        "time_of_day_et": local.strftime("%H:%M:%S"),
        "weekday": local.strftime("%A"),
    }
    try:
        session = exchange_calendar.session_for(local.date())
    except ValueError:
        return info
    info["minutes_since_open"] = round((as_of - session.open_utc).total_seconds() / 60, 2)
    info["minutes_to_close"] = round((session.close_utc - as_of).total_seconds() / 60, 2)
    info["early_close"] = session.is_early_close
    return info


def build_review_request(
    proposal: TradeProposal,
    context: MarketContext,
    *,
    candle_count: int = 30,
    daily_candle_count: int = 5,
) -> dict[str, Any]:
    """The compact, JSON-safe request for ``proposal``; raises ``ReviewLookAheadError`` on leaks."""
    as_of = proposal.as_of.astimezone(UTC)
    if context.symbol != proposal.symbol:
        raise ValueError("context symbol does not match the proposal")
    candles: dict[str, list[dict[str, Any]]] = {}
    latest_ends: dict[str, str | None] = {}
    for timeframe, limit in (
        (CandleTimeframe.M1, candle_count),
        (CandleTimeframe.M5, candle_count),
        (CandleTimeframe.D1, daily_candle_count),
    ):
        series = context.candles.get(timeframe, ())
        if not series or limit < 1:
            continue
        rows = [_candle_row(candle, as_of) for candle in series[-limit:]]
        candles[timeframe.value] = rows
        latest_ends[timeframe.value] = rows[-1]["end"]

    freshness = context.freshness
    quote = context.latest_quote
    trade = context.latest_trade
    market: dict[str, Any] = {
        **_session_clock(as_of),
        "session_status": context.session_status.value,
        "session_id": context.session_id,
        "candles": candles,
        "indicators": {
            key: str(value) for key, value in sorted(context.indicators.items())
            if value is not None
        },
        # Snapshots stamped after the decision time are omitted, never shown.
        "quote": None
        if quote is None or quote.provider_ts > as_of
        else {
            "bid": str(quote.bid), "ask": str(quote.ask),
            "bid_size": None if quote.bid_size is None else str(quote.bid_size),
            "ask_size": None if quote.ask_size is None else str(quote.ask_size),
            "provider_ts": _iso(quote.provider_ts),
        },
        "last_trade": None
        if trade is None or trade.provider_ts > as_of
        else {
            "price": str(trade.price),
            "size": None if trade.size is None else str(trade.size),
            "provider_ts": _iso(trade.provider_ts),
        },
    }
    provenance = {
        "feed": context.feed_id,
        "consolidated": context.consolidated,
        "price_basis": context.price_basis.value,
        "data_as_of": _iso(as_of),
        "newest_data_ts": _iso(freshness.newest_data_ts),
        "data_age_seconds": None if freshness.age is None else freshness.age.total_seconds(),
        "stale": freshness.stale,
        "latest_candle_end": latest_ends,
        "context_version": context.version,
    }

    contributors = []
    for item in proposal.contributors:
        contributors.append(
            {
                "strategy_id": item.strategy_id,
                "strategy_version": item.strategy_version,
                "signal_id": item.signal_id,
                "selected": item.selected,
                "evidence": None if item.evidence is None else item.evidence.to_dict(),
            }
        )
    body: dict[str, Any] = {
        "protocol": REVIEW_PROTOCOL,
        "schema_version": REQUEST_SCHEMA_VERSION,
        "proposal": {
            "action": proposal.action,
            "symbol": proposal.symbol,
            "decision_time": _iso(as_of),
            "execution_mode": proposal.mode,
            "selected_strategy_id": proposal.selected_strategy_id,
            "selected_signal_id": proposal.selected_signal_id,
            "originating_strategy_ids": list(proposal.strategy_ids),
            "reference_price": None
            if proposal.reference_price is None
            else str(proposal.reference_price),
        },
        "strategies": contributors,
        "market": market,
        "provenance": provenance,
    }
    if proposal.position is not None:
        position = proposal.position
        body["position"] = {
            "direction": position.direction,
            "quantity": str(position.quantity),
            "entry_price": str(position.entry_price),
            "entry_time": _iso(position.entry_time),
            "holding_minutes": round((as_of - position.entry_time).total_seconds() / 60, 2),
        }
        last = _last_price(context, as_of)
        if last is not None and position.quantity > _ZERO:
            price, source = last
            pnl = (price - position.entry_price) * position.quantity
            body["position"].update(
                {
                    "mark_price": str(price),
                    "mark_source": source,
                    "unrealized_pnl": str(pnl),
                    "unrealized_pnl_percent": str(
                        (price - position.entry_price) / position.entry_price * Decimal(100)
                    ),
                }
            )
    if proposal.stop is not None:
        stop = proposal.stop
        body["protective_stop"] = {
            "kind": stop.kind,
            "initial_stop": None if stop.initial_stop is None else str(stop.initial_stop),
            "current_stop": None if stop.current_stop is None else str(stop.current_stop),
            "trail_percent": None if stop.trail_percent is None else str(stop.trail_percent),
            "broker_held": stop.broker_held,
            "note": "the stop remains in force regardless of this review",
        }
    return to_json_value(body, path="request")  # type: ignore[no-any-return]


def request_hash(request: Mapping[str, Any]) -> str:
    """Stable SHA-256 of the complete review input (the cache and replay key)."""
    import hashlib

    return hashlib.sha256(canonical_json(request).encode("utf-8")).hexdigest()
