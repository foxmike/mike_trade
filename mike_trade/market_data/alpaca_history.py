"""Bounded, provenance-aware Alpaca historical stock bar acquisition."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

__all__ = [
    "Adjustment",
    "HistoricalBar",
    "AlpacaHistoryError",
    "AlpacaHistoryResponseError",
    "BarProvenanceError",
    "PaginationLimitError",
    "AlpacaHistoricalBars",
    "merge_bars",
]


class Adjustment(StrEnum):
    RAW = "raw"
    SPLIT = "split"
    DIVIDEND = "dividend"
    ALL = "all"


class AlpacaHistoryError(ValueError):
    """Invalid or incomplete Alpaca historical data response."""


class AlpacaHistoryResponseError(AlpacaHistoryError):
    """Sanitized Alpaca HTTP/provider error suitable for logs and retry policy."""

    def __init__(
        self,
        *,
        status_code: int | None,
        provider_code: object = None,
        provider_message: object = None,
        retry_after: float | None = None,
    ) -> None:
        self.status_code = status_code
        self.provider_code = None if provider_code is None else str(provider_code)[:80]
        message = "provider returned an error without a message"
        if provider_message is not None:
            message = " ".join(str(provider_message).split())[:300]
        self.provider_message = message
        self.retry_after = retry_after
        inferred_status: int | None = None
        if status_code is None and self.provider_code is not None:
            prefix = self.provider_code[:3]
            if prefix.isdigit():
                inferred_status = int(prefix)
        effective_status = status_code if status_code is not None else inferred_status
        self.retryable = effective_status == 429 or (
            effective_status is not None and 500 <= effective_status <= 599
        )
        status = "unknown" if status_code is None else str(status_code)
        code = f" code={self.provider_code}" if self.provider_code is not None else ""
        super().__init__(f"Alpaca historical API error: HTTP {status}{code} message={message}")


class BarProvenanceError(AlpacaHistoryError):
    """Bars in a request or merge do not share feed and adjustment provenance."""


class PaginationLimitError(AlpacaHistoryError):
    """The response still has another page after the configured page limit."""


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{field} must be numeric") from None
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


def _count(value: Any, field: str) -> int | Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be integer or Decimal")
    if isinstance(value, float):
        return _decimal(value, field)
    if isinstance(value, int):
        return value
    return _decimal(value, field)


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("bar timestamp must be an ISO timestamp string")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return _utc(datetime.fromisoformat(text), "bar timestamp")
    except ValueError as exc:
        raise ValueError("bar timestamp must be ISO formatted and timezone-aware") from exc


@dataclass(frozen=True, slots=True)
class HistoricalBar:
    symbol: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int | Decimal
    vwap: Decimal | None
    trade_count: int | Decimal | None
    feed_id: str
    adjustment: Adjustment

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper() if isinstance(self.symbol, str) else ""
        if not symbol:
            raise ValueError("symbol must be non-empty")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "timestamp", _utc(self.timestamp, "timestamp"))
        for field in ("open", "high", "low", "close"):
            object.__setattr__(self, field, _decimal(getattr(self, field), field))
        object.__setattr__(self, "volume", _count(self.volume, "volume"))
        if self.vwap is not None:
            object.__setattr__(self, "vwap", _decimal(self.vwap, "vwap"))
        if self.trade_count is not None:
            object.__setattr__(self, "trade_count", _count(self.trade_count, "trade_count"))
        if not isinstance(self.feed_id, str) or not self.feed_id.strip():
            raise ValueError("feed_id must be non-empty")
        object.__setattr__(self, "feed_id", self.feed_id.strip().lower())
        if not isinstance(self.adjustment, Adjustment):
            object.__setattr__(self, "adjustment", Adjustment(self.adjustment))

    @property
    def adjustment_provenance(self) -> Adjustment:
        """Explicitly named alias for the requested adjustment mode."""
        return self.adjustment


def merge_bars(
    *groups: tuple[HistoricalBar, ...] | list[HistoricalBar],
) -> tuple[HistoricalBar, ...]:
    """Merge bars only when every input shares the exact feed and adjustment."""
    bars = [bar for group in groups for bar in group]
    if not bars:
        return ()
    provenance = {(bar.feed_id, bar.adjustment) for bar in bars}
    if len(provenance) != 1:
        raise BarProvenanceError("cannot merge bars from different feeds or adjustments")
    return tuple(sorted(bars, key=lambda bar: (bar.timestamp, bar.symbol)))


class AlpacaHistoricalBars:
    """Fetch historical bars using an injected object exposing ``get(path, params)``."""

    def __init__(self, client: Any, *, max_pages: int = 20, page_limit: int | None = None) -> None:
        if not callable(getattr(client, "get", None)):
            raise TypeError("client must expose get(path, params)")
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
            raise ValueError("max_pages must be a positive integer")
        if page_limit is not None and (
            isinstance(page_limit, bool)
            or not isinstance(page_limit, int)
            or not 1 <= page_limit <= 10000
        ):
            raise ValueError("page_limit must be between 1 and 10000")
        self._client = client
        self.max_pages = max_pages
        self.page_limit = page_limit

    def fetch_bars(
        self,
        symbol: str,
        timeframe: Any,
        start: datetime,
        end: datetime,
        *,
        feed: str = "iex",
        adjustment: Adjustment | str = Adjustment.RAW,
    ) -> tuple[HistoricalBar, ...]:
        symbol = symbol.strip().upper()
        feed = feed.strip().lower()
        try:
            adjustment = Adjustment(adjustment)
        except ValueError:
            raise ValueError("adjustment must be raw, split, dividend, or all") from None
        start_utc, end_utc = _utc(start, "start"), _utc(end, "end")
        if start_utc >= end_utc:
            raise ValueError("start must be before end")
        timeframe_value = getattr(timeframe, "value", timeframe)
        if (
            not symbol
            or not feed
            or not isinstance(timeframe_value, str)
            or not timeframe_value.strip()
        ):
            raise ValueError("symbol, timeframe, and feed must be non-empty")
        timeframe_value = {
            "1m": "1Min",
            "5m": "5Min",
            "15m": "15Min",
            "1h": "1Hour",
            "1d": "1Day",
        }.get(timeframe_value.strip().lower(), timeframe_value.strip())
        params: dict[str, Any] = {
            "symbols": symbol,
            "timeframe": timeframe_value,
            "start": start_utc.isoformat().replace("+00:00", "Z"),
            "end": end_utc.isoformat().replace("+00:00", "Z"),
            "feed": feed,
            "adjustment": adjustment.value,
        }
        if self.page_limit is not None:
            params["limit"] = self.page_limit
        token: str | None = None
        seen_tokens: set[str] = set()
        bars: list[HistoricalBar] = []
        for _page in range(self.max_pages):
            if token is not None:
                params["page_token"] = token
            data = self._client.get("/v2/stocks/bars", params=params.copy())
            if not isinstance(data, Mapping):
                raise AlpacaHistoryError("historical bars response must be an object")
            if "bars" not in data and ("code" in data or "message" in data):
                raise AlpacaHistoryResponseError(
                    status_code=None,
                    provider_code=data.get("code"),
                    provider_message=data.get("message"),
                )
            response_feed = data.get("feed")
            response_adjustment = data.get("adjustment")
            if response_feed is not None and str(response_feed).lower() != feed:
                raise BarProvenanceError("response feed differs from requested feed")
            if (
                response_adjustment is not None
                and str(response_adjustment).lower() != adjustment.value
            ):
                raise BarProvenanceError("response adjustment differs from requested adjustment")
            if "bars" not in data:
                keys = ",".join(sorted(str(key) for key in data)[:10]) or "none"
                raise AlpacaHistoryError(
                    f"malformed historical bars response; keys={keys}"
                )
            result = data["bars"]
            if isinstance(result, Mapping):
                # A valid multi-symbol response may omit a symbol when it has no trades.
                result = result.get(symbol, [])
            if not isinstance(result, list):
                raise AlpacaHistoryError("historical bars response must contain a bars array")
            for item in result:
                if not isinstance(item, Mapping):
                    raise AlpacaHistoryError("bar item must be an object")
                item_feed = item.get("feed_id", item.get("feed", feed))
                item_adjustment = item.get("adjustment", adjustment.value)
                if (
                    str(item_feed).lower() != feed
                    or str(item_adjustment).lower() != adjustment.value
                ):
                    raise BarProvenanceError("bar provenance differs from request")
                bar = (
                    HistoricalBar(
                        symbol=symbol,
                        timestamp=_timestamp(item.get("t")),
                        open=_decimal(item.get("o"), "open"),
                        high=_decimal(item.get("h"), "high"),
                        low=_decimal(item.get("l"), "low"),
                        close=_decimal(item.get("c"), "close"),
                        volume=_count(item.get("v"), "volume"),
                        vwap=None if item.get("vw") is None else _decimal(item["vw"], "vwap"),
                        trade_count=None
                        if item.get("n") is None
                        else _count(item["n"], "trade_count"),
                        feed_id=feed,
                        adjustment=adjustment,
                    )
                )
                # Alpaca documents an inclusive end; research requests use
                # half-open windows so adjoining windows never overlap.
                if start_utc <= bar.timestamp < end_utc:
                    bars.append(bar)
            token = data.get("next_page_token")
            if not token:
                break
            if not isinstance(token, str) or token in seen_tokens:
                raise PaginationLimitError("historical pagination token repeated or invalid")
            seen_tokens.add(token)
        else:
            raise PaginationLimitError(f"historical response exceeds max_pages={self.max_pages}")
        return tuple(sorted(bars, key=lambda bar: bar.timestamp))
