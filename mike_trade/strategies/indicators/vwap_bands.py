"""Pure Decimal point-in-time session VWAP deviation bands calculator.

Computes cumulative volume-weighted average price (VWAP) and volume-weighted
standard deviation bands for regular intraday trading sessions. All arithmetic
runs in a fresh Decimal context with precision 28 and ROUND_HALF_EVEN rounding.

Exact Mathematical Formula:
    For bars i = 0 .. k in the current session:
        TP_i = (High_i + Low_i + Close_i) / 3
        V_i = Volume_i

    Accumulators (reset to zero at session start):
        CumV_k = sum_{i=0..k} V_i
        CumVP_k = sum_{i=0..k} (TP_i * V_i)
        CumVPSq_k = sum_{i=0..k} (TP_i^2 * V_i)

    Point-in-Time Outputs:
        VWAP_k = CumVP_k / CumV_k
        Variance_k = max(0, (CumVPSq_k / CumV_k) - VWAP_k^2)
        StdDev_k = sqrt(Variance_k)
        LowerBand_k = VWAP_k - multiplier * StdDev_k
        UpperBand_k = VWAP_k + multiplier * StdDev_k

Zero-volume bars do not increment accumulators. If CumV == 0, returns None.

Price basis and consistency: ``VWAP_k`` uses exactly the typical price, the volume gating
and the arithmetic context of the production session VWAP (``core.VWAPCalculator``, the
calculator behind the context ``vwap`` / ``vwap_prev`` indicators), so the two agree
bit-for-bit on the same bars.  The deviation is the volume-weighted *population* standard
deviation of the typical price about that VWAP (the common charting-platform "standard
deviation bands" construction); it is a dispersion of bar typical prices, not of
individual trades.

Session identity: when the bars (or the caller) carry no session identity the
accumulators are never reset, i.e. the supplied bars are treated as one session.  Pass a
``session_id`` (see :func:`session_key`) to reset on a change of session.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import NamedTuple
from zoneinfo import ZoneInfo

from mike_trade.market_data.candles import Candle
from mike_trade.strategies.indicators.core import (
    _UNSET,
    _ZERO,
    BarLike,
    _arithmetic,
    _decimal,
    _load,
    session_vwap,
)

__all__ = [
    "VWAPBands",
    "VWAPDeviationBandsCalculator",
    "session_vwap_bands",
    "session_key",
    "session_vwap_series",
    "session_bands_series",
    "vwap_matches",
]

_NEW_YORK = ZoneInfo("America/New_York")


class VWAPBands(NamedTuple):
    """Point-in-time session VWAP and deviation bands."""

    vwap: Decimal
    lower: Decimal
    upper: Decimal
    std_dev: Decimal


class VWAPDeviationBandsCalculator:
    """Intraday volume-weighted VWAP deviation bands calculator.

    Accumulators reset whenever session_id changes. Bars are processed point-in-time
    with no future knowledge.
    """

    def __init__(self, multiplier: Decimal = Decimal("2.0")) -> None:
        multiplier = _decimal(multiplier, "multiplier")
        if multiplier < _ZERO:
            raise ValueError("multiplier must be non-negative")
        self.multiplier = multiplier
        self.warmup_bars = 1
        self._session_id: object = _UNSET
        self._cum_volume = _ZERO
        self._cum_weighted_price = _ZERO
        self._cum_weighted_price_sq = _ZERO

    @property
    def ready(self) -> bool:
        return self._cum_volume > 0

    def reset(self, session_id: object = _UNSET) -> None:
        """Reset session accumulators."""
        self._session_id = session_id
        self._cum_volume = _ZERO
        self._cum_weighted_price = _ZERO
        self._cum_weighted_price_sq = _ZERO

    def update(self, bar: BarLike, session_id: object = _UNSET) -> VWAPBands | None:
        """Update accumulators with the completed bar and return point-in-time bands."""
        if session_id is _UNSET:
            session_id = getattr(bar, "session_id", _UNSET)
        # Reset only when a session identity is known and has changed.  Bars that carry no
        # identity continue the current accumulators (they used to reset on every bar, which
        # collapsed the bands onto each bar's own typical price).
        if session_id is not _UNSET and session_id != self._session_id:
            self.reset(session_id)

        high = _decimal(getattr(bar, "high"), "high")
        low = _decimal(getattr(bar, "low"), "low")
        close = _decimal(getattr(bar, "close"), "close")
        volume = _decimal(getattr(bar, "volume"), "volume")

        if volume < 0:
            raise ValueError("volume must be >= 0")
        if high < low:
            raise ValueError("high must be >= low")

        if volume > 0:
            with _arithmetic():
                typical = (high + low + close) / Decimal("3")
                self._cum_volume += volume
                self._cum_weighted_price += typical * volume
                self._cum_weighted_price_sq += typical * typical * volume

        if self._cum_volume == 0:
            return None

        with _arithmetic():
            vwap = self._cum_weighted_price / self._cum_volume
            variance = (self._cum_weighted_price_sq / self._cum_volume) - (vwap * vwap)
            if variance < _ZERO:
                variance = _ZERO
            std_dev = variance.sqrt()
            lower = vwap - self.multiplier * std_dev
            upper = vwap + self.multiplier * std_dev

        return VWAPBands(vwap=vwap, lower=lower, upper=upper, std_dev=std_dev)

    def snapshot(self) -> dict[str, object]:
        """Serialize internal state for persistence (JSON-safe)."""
        if self._session_id is not _UNSET and not isinstance(self._session_id, str):
            raise TypeError("session_id must be a str to be snapshotted")
        return {
            "multiplier": str(self.multiplier),
            "session_id": None if self._session_id is _UNSET else self._session_id,
            "cum_volume": str(self._cum_volume),
            "cum_weighted_price": str(self._cum_weighted_price),
            "cum_weighted_price_sq": str(self._cum_weighted_price_sq),
        }

    @classmethod
    def restore(cls, snapshot: Mapping[str, object]) -> VWAPDeviationBandsCalculator:
        """Restore calculator from serialized snapshot; a malformed snapshot raises."""
        multiplier = _load(snapshot["multiplier"])
        cum_volume = _load(snapshot["cum_volume"])
        cum_weighted = _load(snapshot["cum_weighted_price"])
        cum_weighted_sq = _load(snapshot["cum_weighted_price_sq"])
        if multiplier is None or cum_volume is None or cum_weighted is None or cum_weighted_sq is None:
            raise ValueError("incomplete VWAP bands snapshot")
        if cum_volume < _ZERO or cum_weighted < _ZERO or cum_weighted_sq < _ZERO:
            raise ValueError("invalid VWAP bands snapshot")
        calc = cls(multiplier=multiplier)  # a multiplier of 0 is valid and must be preserved
        session_id = snapshot["session_id"]
        if session_id is not None and not isinstance(session_id, str):
            raise ValueError("invalid VWAP bands snapshot session_id")
        calc._session_id = _UNSET if session_id is None else session_id
        calc._cum_volume = cum_volume
        calc._cum_weighted_price = cum_weighted
        calc._cum_weighted_price_sq = cum_weighted_sq
        return calc


def session_vwap_bands(
    bars: Iterable[BarLike],
    multiplier: Decimal = Decimal("2.0"),
) -> list[VWAPBands | None]:
    """Calculate point-in-time VWAP deviation bands for a sequence of bars."""
    calc = VWAPDeviationBandsCalculator(multiplier=multiplier)
    return [calc.update(bar) for bar in bars]


# ----------------------------------------------------- session-aligned series for strategies


@dataclass(frozen=True, slots=True)
class _SessionBar:
    open: Decimal  # noqa: A003 - OHLC field names
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    session_id: str


def session_key(candle: Candle) -> str:
    """The New York session date of ``candle`` (the session identity used for resets)."""
    return candle.start.astimezone(_NEW_YORK).date().isoformat()


def session_vwap_series(candles: Sequence[Candle]) -> list[Decimal | None]:
    """Session VWAP *as of each candle's own close*, using the production calculator.

    This is the series the shared ``vwap`` / ``vwap_prev`` context indicators are the last
    two points of (``core.session_vwap`` over session-tagged bars), reset at each New York
    session date.  Entry ``i`` uses only candles ``0..i``, so comparing candle ``i`` with entry
    ``i`` is point-in-time.  It equals the production value only if ``candles`` starts at the
    session open; see :func:`vwap_matches`.
    """
    return session_vwap(
        _SessionBar(c.open, c.high, c.low, c.close, c.volume, session_key(c)) for c in candles
    )


def session_bands_series(
    candles: Sequence[Candle], multiplier: Decimal = Decimal("2.0")
) -> list[VWAPBands | None]:
    """Point-in-time VWAP deviation bands as of each candle's own close (session aware)."""
    calc = VWAPDeviationBandsCalculator(multiplier=multiplier)
    return [calc.update(c, session_key(c)) for c in candles]


def vwap_matches(
    computed: Decimal | None, service: object, relative_tolerance: Decimal = Decimal("1e-9")
) -> bool:
    """True when a recomputed session VWAP agrees with the context's shared indicator value.

    Strategies recompute a per-bar series from the supplied candles; the context's ``vwap`` /
    ``vwap_prev`` were accumulated from the whole session.  A disagreement means the supplied
    history does not reach the session open (or the indicator service saw different bars), so
    the recomputed series is not the production VWAP and the strategy must not trade on it.
    """
    if computed is None or not isinstance(service, Decimal) or not service.is_finite():
        return False
    if service <= 0:
        return False
    return abs(computed - service) <= relative_tolerance * service
