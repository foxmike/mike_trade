"""Bounded candle and indicator warmup state diagnostics (new entries only).

This module tracks per-(symbol, timeframe) warmup from declared bar-count needs
plus h.2 indicator ``warmup_bars`` metadata, using only bounded buffers
(``collections.deque`` with ``maxlen``).  It never stores an unbounded bar
history and performs no network, provider, or broker I/O.

The gate exposed here (:meth:`WarmupTracker.buy_gate`) applies to **new
entries only**.  This module exposes **no API that blocks exits, fills,
stops, reconciliation, or manual closes** -- there is no ``exit_gate``,
``close_gate``, ``fill_gate``, ``stop_gate``, ``reconcile``/``reconciliation``,
``manual_close``, ``cancel``, or order-management API of any kind.  Consuming
code must never consult warmup state to delay or veto an exit, a fill, a
stop, reconciliation, or a manual close.

Inputs are consumed through public APIs only: h.1 ``Candle`` / ``CandleStatus``
/ ``CandleTimeframe`` / ``PriceBasis``, h.4 ``MarketContext`` snapshots, h.2
calculator ``warmup_bars`` attributes, and h.3 session info via the calendar
object's public ``session_id`` / ``session_status`` methods.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any

from mike_trade.market_data.candles import CandleStatus, CandleTimeframe, PriceBasis
from mike_trade.scheduling.calendar import SessionStatus

__all__ = [
    "WarmupState",
    "WarmupRequirement",
    "RequirementStatus",
    "SymbolWarmup",
    "WarmupDiagnostics",
    "GateDecision",
    "WarmupTracker",
    "REASON_INSUFFICIENT_BARS",
    "REASON_STALE_DATA",
    "REASON_MISSING_DATA",
    "REASON_BAR_GAP",
    "REASON_ZERO_VOLUME",
    "REASON_INVALID_OHLC",
    "REASON_MIXED_FEED",
    "REASON_UNCLOSED_BAR",
    "REASON_PRICE_BASIS_MISMATCH",
    "REASON_SESSION_NOT_REGULAR",
    "REASON_UNKNOWN_SESSION",
    "REASON_CODES",
]

REASON_INSUFFICIENT_BARS = "INSUFFICIENT_BARS"
REASON_STALE_DATA = "STALE_DATA"
REASON_MISSING_DATA = "MISSING_DATA"
REASON_BAR_GAP = "BAR_GAP"
REASON_ZERO_VOLUME = "ZERO_VOLUME"
REASON_INVALID_OHLC = "INVALID_OHLC"
REASON_MIXED_FEED = "MIXED_FEED"
REASON_UNCLOSED_BAR = "UNCLOSED_BAR"
REASON_PRICE_BASIS_MISMATCH = "PRICE_BASIS_MISMATCH"
REASON_SESSION_NOT_REGULAR = "SESSION_NOT_REGULAR"
REASON_UNKNOWN_SESSION = "UNKNOWN_SESSION"

REASON_CODES = (
    REASON_INSUFFICIENT_BARS,
    REASON_STALE_DATA,
    REASON_MISSING_DATA,
    REASON_BAR_GAP,
    REASON_ZERO_VOLUME,
    REASON_INVALID_OHLC,
    REASON_MIXED_FEED,
    REASON_UNCLOSED_BAR,
    REASON_PRICE_BASIS_MISMATCH,
    REASON_SESSION_NOT_REGULAR,
    REASON_UNKNOWN_SESSION,
)

_INTEGRITY_REASONS = frozenset(
    {
        REASON_INVALID_OHLC,
        REASON_UNCLOSED_BAR,
        REASON_PRICE_BASIS_MISMATCH,
        REASON_MIXED_FEED,
    }
)


class WarmupState(str, Enum):
    COLD = "COLD"
    WARMING = "WARMING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    INVALID = "INVALID"


def _norm_symbol(value: object) -> str:
    text = value.strip().upper() if isinstance(value, str) else ""
    if not text:
        raise ValueError("symbol must be a non-empty str")
    return text


def _norm_feed(value: object) -> str:
    text = value.strip().lower() if isinstance(value, str) else ""
    if not text:
        raise ValueError("feed_id must be a non-empty str")
    return text


@dataclass(frozen=True, slots=True)
class WarmupRequirement:
    """One declared readiness need.

    ``bars_needed`` is the declared completed-bar count; when
    ``indicator_factory`` is given, the effective need is
    ``max(bars_needed, factory().warmup_bars)`` so h.2 indicator warmup
    metadata participates without inventing thresholds.  ``volume_relevant``
    marks requirements for which a zero-volume bar blocks.  ``session_scoped``
    marks requirements (e.g. session VWAP) that count only bars from the
    current ``session_id`` and therefore reset at a session change.
    """

    name: str
    timeframe: CandleTimeframe
    bars_needed: int
    volume_relevant: bool = False
    session_scoped: bool = False
    indicator_factory: Callable[[], Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("name must be a non-empty str")
        if not isinstance(self.timeframe, CandleTimeframe):
            raise ValueError("timeframe must be CandleTimeframe")
        if (
            isinstance(self.bars_needed, bool)
            or not isinstance(self.bars_needed, int)
            or self.bars_needed < 1
        ):
            raise ValueError("bars_needed must be a positive int")
        if not isinstance(self.volume_relevant, bool):
            raise ValueError("volume_relevant must be bool")
        if not isinstance(self.session_scoped, bool):
            raise ValueError("session_scoped must be bool")
        if self.indicator_factory is not None and not callable(self.indicator_factory):
            raise ValueError("indicator_factory must be callable or None")

    @property
    def effective_bars_needed(self) -> int:
        need = self.bars_needed
        if self.indicator_factory is not None:
            calc = self.indicator_factory()
            warmup = getattr(calc, "warmup_bars", 0)
            if isinstance(warmup, bool) or not isinstance(warmup, int) or warmup < 0:
                raise ValueError("indicator warmup_bars must be a non-negative int")
            need = max(need, warmup)
        return need


@dataclass(frozen=True, slots=True)
class RequirementStatus:
    name: str
    timeframe: CandleTimeframe
    bars_have: int
    bars_needed: int
    state: WarmupState
    reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class SymbolWarmup:
    symbol: str
    requirements: tuple[RequirementStatus, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class WarmupDiagnostics:
    """Immutable snapshot of warmup state; mutation raises (frozen dataclass)."""

    as_of: datetime
    symbols: tuple[SymbolWarmup, ...] = field(default_factory=tuple)

    def for_symbol(self, symbol: str) -> SymbolWarmup | None:
        key = _norm_symbol(symbol)
        for entry in self.symbols:
            if entry.symbol == key:
                return entry
        return None


@dataclass(frozen=True, slots=True)
class GateDecision:
    """New-entry gate outcome.  ``allowed`` is True only when every declared
    requirement is READY and no blocking reason codes are present."""

    symbol: str
    allowed: bool
    reason_codes: tuple[str, ...] = field(default_factory=tuple)


def _valid_ohlc(bar: Any) -> bool:
    try:
        o, h, lo, c = bar.open, bar.high, bar.low, bar.close
        for v in (o, h, lo, c):
            if not isinstance(v, Decimal) or not v.is_finite() or v <= 0:
                return False
        return bool(h >= lo and lo <= o <= h and lo <= c <= h)
    except Exception:
        return False


def _is_zero_volume(bar: Any) -> bool:
    try:
        return bar.volume == 0
    except Exception:
        return False


def _sorted_bars(bars: Iterable[Any]) -> list[Any]:
    return sorted(bars, key=lambda b: b.start)


def _has_gap(bars: Sequence[Any]) -> bool:
    ordered = _sorted_bars(bars)
    for prev, cur in zip(ordered, ordered[1:]):
        try:
            if cur.start != prev.end:
                return True
        except Exception:
            return True
    return False


def _state_for(have: int, need: int, reasons: tuple[str, ...]) -> WarmupState:
    if any(r in _INTEGRITY_REASONS for r in reasons):
        return WarmupState.INVALID
    if have < need:
        return WarmupState.COLD if have == 0 else WarmupState.WARMING
    if reasons:
        return WarmupState.DEGRADED
    return WarmupState.READY


class WarmupTracker:
    """Bounded per-(symbol, timeframe) warmup tracker with a new-entry buy gate.

    Only :meth:`buy_gate` expresses entry eligibility, and only for new
    entries.  No method here can block exits, fills, stops, reconciliation,
    or manual closes.
    """

    def __init__(
        self,
        *,
        clock: Any,
        requirements: Mapping[str, Sequence[WarmupRequirement]],
        expected_feed_id: str | None = None,
        expected_basis: PriceBasis = PriceBasis.RAW,
        calendar: Any | None = None,
    ) -> None:
        if not callable(getattr(clock, "now", None)):
            raise ValueError("clock must expose now()")
        if not isinstance(requirements, Mapping) or not requirements:
            raise ValueError("requirements must be a non-empty mapping of symbol to needs")
        if not isinstance(expected_basis, PriceBasis):
            raise ValueError("expected_basis must be PriceBasis")
        self._clock = clock
        self._expected_feed = _norm_feed(expected_feed_id) if expected_feed_id is not None else None
        self._expected_basis = expected_basis
        if calendar is None:
            from mike_trade.market_data.context import NyseCalendar

            calendar = NyseCalendar()
        if not callable(getattr(calendar, "session_id", None)):
            raise ValueError("calendar must expose session_id()")
        self._calendar = calendar
        self._requirements: dict[str, tuple[WarmupRequirement, ...]] = {}
        for symbol, seq in requirements.items():
            key = _norm_symbol(symbol)
            needs = tuple(seq)
            if not needs or any(not isinstance(r, WarmupRequirement) for r in needs):
                raise ValueError(f"requirements[{key!r}] must be a non-empty sequence")
            names = [r.name for r in needs]
            if len(set(names)) != len(names):
                raise ValueError(f"requirements[{key!r}] names must be unique")
            self._requirements[key] = needs
        self._buffers: dict[tuple[str, CandleTimeframe], deque] = {}
        self._bounds: dict[tuple[str, CandleTimeframe], int] = {}
        for symbol, needs in self._requirements.items():
            for req in needs:
                key = (symbol, req.timeframe)
                need = req.effective_bars_needed
                self._bounds[key] = max(self._bounds.get(key, 0), need)
        self._fallback_bound = max(self._bounds.values())
        for key, bound in self._bounds.items():
            self._buffers[key] = deque(maxlen=bound)
        self._feed_lock: dict[str, str] = {}
        self._flags: dict[str, set[str]] = {}

    # -- ingestion ------------------------------------------------------

    def _flag(self, symbol: str, reason: str) -> None:
        self._flags.setdefault(symbol, set()).add(reason)

    def _bound_for(self, symbol: str, timeframe: CandleTimeframe) -> int:
        return self._bounds.get((symbol, timeframe), self._fallback_bound)

    def _buffer_for(self, symbol: str, timeframe: CandleTimeframe) -> deque:
        key = (symbol, timeframe)
        buf = self._buffers.get(key)
        if buf is None:
            bound = self._fallback_bound
            self._bounds[key] = bound
            buf = self._buffers[key] = deque(maxlen=bound)
        return buf

    def add_bar(self, bar: Any) -> None:
        """Ingest one completed bar (live path)."""
        symbol = _norm_symbol(getattr(bar, "symbol", ""))
        timeframe = getattr(bar, "timeframe", None)
        if not isinstance(timeframe, CandleTimeframe):
            raise ValueError("bar.timeframe must be CandleTimeframe")
        if getattr(bar, "status", None) is not CandleStatus.CLOSED:
            self._flag(symbol, REASON_UNCLOSED_BAR)
            return
        if not _valid_ohlc(bar):
            self._flag(symbol, REASON_INVALID_OHLC)
            return
        basis = getattr(bar, "price_basis", None)
        if basis is not self._expected_basis:
            self._flag(symbol, REASON_PRICE_BASIS_MISMATCH)
            return
        feed = _norm_feed(getattr(bar, "feed_id", ""))
        locked = self._feed_lock.get(symbol)
        if locked is None:
            self._feed_lock[symbol] = feed
        elif locked != feed:
            self._flag(symbol, REASON_MIXED_FEED)
            return
        self._buffer_for(symbol, timeframe).append(bar)

    def seed_historical(self, bars: Iterable[Any]) -> None:
        """Seed from historical completed bars; identical state to live feeding."""
        for bar in bars:
            self.add_bar(bar)

    def update_from_context(self, context: Any) -> None:
        """Ingest the closed candles held by a ``MarketContext`` snapshot."""
        if context is None:
            raise ValueError("context must not be None")
        candles = getattr(context, "candles", {})
        for timeframe, seq in dict(candles).items():
            for bar in tuple(seq):
                self.add_bar(bar)

    # -- evaluation -----------------------------------------------------

    def _session_of(self, bar: Any) -> str | None:
        try:
            return self._calendar.session_id(getattr(bar, "start"))
        except Exception:
            return None

    def _stored_eval(
        self, symbol: str, req: WarmupRequirement, current_session: str | None
    ) -> RequirementStatus:
        buf = self._buffers.get((symbol, req.timeframe), deque())
        bars = list(buf)
        if req.session_scoped and current_session is not None:
            bars = [b for b in bars if self._session_of(b) == current_session]
        need = req.effective_bars_needed
        have = len(bars)
        reasons: list[str] = []
        if have < need:
            reasons.append(REASON_INSUFFICIENT_BARS)
            if have == 0:
                reasons.append(REASON_MISSING_DATA)
        for flag in sorted(self._flags.get(symbol, ())):
            if flag not in reasons:
                reasons.append(flag)
        if _has_gap(bars) and REASON_BAR_GAP not in reasons:
            reasons.append(REASON_BAR_GAP)
        if req.volume_relevant and any(_is_zero_volume(b) for b in bars):
            if REASON_ZERO_VOLUME not in reasons:
                reasons.append(REASON_ZERO_VOLUME)
        ordered = tuple(dict.fromkeys(reasons))
        return RequirementStatus(req.name, req.timeframe, have, need, _state_for(have, need, ordered), ordered)

    def _context_reasons(self, symbol: str, context: Any) -> tuple[str, ...]:
        if context is None:
            return (REASON_MISSING_DATA,)
        reasons: list[str] = []
        freshness = getattr(context, "freshness", None)
        stale = bool(getattr(freshness, "stale", False)) or bool(
            getattr(context, "is_stale", False)
        )
        if stale:
            reasons.append(REASON_STALE_DATA)
        status = getattr(context, "session_status", None)
        if status is SessionStatus.UNKNOWN:
            reasons.append(REASON_UNKNOWN_SESSION)
        elif status is not SessionStatus.REGULAR:
            reasons.append(REASON_SESSION_NOT_REGULAR)
        candles = getattr(context, "candles", {}) or {}
        seen: list[Any] = [b for seq in candles.values() for b in tuple(seq)]
        for bar in seen:
            if getattr(bar, "status", CandleStatus.CLOSED) is not CandleStatus.CLOSED:
                reasons.append(REASON_UNCLOSED_BAR)
                break
        for bar in seen:
            if not _valid_ohlc(bar):
                reasons.append(REASON_INVALID_OHLC)
                break
        bases = {getattr(b, "price_basis", None) for b in seen}
        if any(b is not self._expected_basis for b in bases):
            reasons.append(REASON_PRICE_BASIS_MISMATCH)
        feeds = {_norm_feed(getattr(b, "feed_id", "?")) for b in seen if getattr(b, "feed_id", None)}
        ctx_feed = getattr(context, "feed_id", None)
        if ctx_feed is not None:
            try:
                feeds.add(_norm_feed(ctx_feed))
            except ValueError:
                pass
        if len(feeds) > 1:
            reasons.append(REASON_MIXED_FEED)
        m1 = [b for b in seen if getattr(b, "timeframe", None) is CandleTimeframe.M1]
        if _has_gap(m1):
            reasons.append(REASON_BAR_GAP)
        return tuple(dict.fromkeys(reasons))

    def _current_session(self, symbol: str, context: Any) -> str | None:
        if context is not None:
            session_id = getattr(context, "session_id", None)
            if isinstance(session_id, str) and session_id:
                return session_id
        buf_bars = [b for (s, _), dq in self._buffers.items() if s == symbol for b in dq]
        if not buf_bars:
            return None
        try:
            latest = max(buf_bars, key=lambda b: b.start)
            return self._session_of(latest)
        except Exception:
            return None

    def diagnostics(self, symbol: str | None = None, context: Any = None) -> WarmupDiagnostics:
        """Return an immutable snapshot.  Same ordered inputs plus the same
        injected-clock reading always produce an equal snapshot."""
        as_of = self._clock.now()
        symbols = sorted(self._requirements) if symbol is None else [_norm_symbol(symbol)]
        entries: list[SymbolWarmup] = []
        for sym in symbols:
            reqs = self._requirements.get(sym, ())
            current = self._current_session(sym, context)
            ctx_reasons = self._context_reasons(sym, context) if context is not None else ()
            statuses: list[RequirementStatus] = []
            for req in reqs:
                stored = self._stored_eval(sym, req, current)
                if ctx_reasons:
                    merged = tuple(dict.fromkeys((*stored.reasons, *ctx_reasons)))
                    # Re-check volume/gap against context candles for volume reqs.
                    statuses.append(
                        RequirementStatus(
                            stored.name, stored.timeframe, stored.bars_have,
                            stored.bars_needed, _state_for(stored.bars_have, stored.bars_needed, merged), merged,
                        )
                    )
                else:
                    statuses.append(stored)
            entries.append(SymbolWarmup(sym, tuple(statuses)))
        return WarmupDiagnostics(as_of, tuple(entries))

    def buy_gate(self, symbol: str, context: Any = None) -> GateDecision:
        """New-entry gate: ``allowed`` only when every declared requirement is
        READY with no blocking reasons; otherwise fails closed."""
        sym = _norm_symbol(symbol)
        reqs = self._requirements.get(sym)
        if not reqs:
            return GateDecision(sym, False, (REASON_MISSING_DATA,))
        current = self._current_session(sym, context)
        ctx_reasons = self._context_reasons(sym, context)
        codes: list[str] = []
        all_ready = True
        for req in reqs:
            stored = self._stored_eval(sym, req, current)
            merged = tuple(dict.fromkeys((*stored.reasons, *ctx_reasons)))
            # Zero-volume / gap faults visible only in the offered context must
            # also block even before the tracker has ingested those bars.
            if context is not None:
                candles = getattr(context, "candles", {}) or {}
                offered = [b for seq in candles.values() for b in tuple(seq)
                           if getattr(b, "timeframe", None) is req.timeframe]
                if req.session_scoped and current is not None:
                    offered = [b for b in offered if self._session_of(b) == current]
                extra: list[str] = []
                if offered and _has_gap(offered) and REASON_BAR_GAP not in merged:
                    extra.append(REASON_BAR_GAP)
                if req.volume_relevant and any(_is_zero_volume(b) for b in offered):
                    if REASON_ZERO_VOLUME not in merged:
                        extra.append(REASON_ZERO_VOLUME)
                if extra:
                    merged = tuple(dict.fromkeys((*merged, *extra)))
            state = _state_for(stored.bars_have, stored.bars_needed, merged)
            if state is not WarmupState.READY:
                all_ready = False
            for code in merged:
                if code not in codes:
                    codes.append(code)
        allowed = all_ready and not codes
        return GateDecision(sym, allowed, tuple(codes))

    # -- bounded-buffer introspection ------------------------------------

    def buffer_size(self, symbol: str, timeframe: CandleTimeframe) -> int:
        return len(self._buffers.get((_norm_symbol(symbol), timeframe), ()))

    def buffer_bound(self, symbol: str, timeframe: CandleTimeframe) -> int:
        return self._bound_for(_norm_symbol(symbol), timeframe)

    def reset(self, symbol: str | None = None) -> None:
        """Clear buffered bars and per-symbol flags/locks (tests/support)."""
        if symbol is None:
            for buf in self._buffers.values():
                buf.clear()
            self._flags.clear()
            self._feed_lock.clear()
            return
        sym = _norm_symbol(symbol)
        for (s, _), buf in self._buffers.items():
            if s == sym:
                buf.clear()
        self._flags.pop(sym, None)
        self._feed_lock.pop(sym, None)
