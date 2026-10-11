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
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import NamedTuple

from mike_trade.strategies.indicators.core import (
    BarLike,
    _arithmetic,
    _decimal,
    _dump,
    _load,
    _UNSET,
    _ZERO,
)

__all__ = [
    "VWAPBands",
    "VWAPDeviationBandsCalculator",
    "session_vwap_bands",
]


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
        if self._session_id is _UNSET or (
            session_id is not _UNSET and session_id != self._session_id
        ):
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
        """Serialize internal state for persistence."""
        return {
            "multiplier": str(self.multiplier),
            "session_id": None if self._session_id is _UNSET else str(self._session_id),
            "cum_volume": str(self._cum_volume),
            "cum_weighted_price": str(self._cum_weighted_price),
            "cum_weighted_price_sq": str(self._cum_weighted_price_sq),
        }

    @classmethod
    def restore(cls, snapshot: Mapping[str, object]) -> VWAPDeviationBandsCalculator:
        """Restore calculator from serialized snapshot."""
        multiplier = _load(snapshot.get("multiplier")) or Decimal("2.0")
        calc = cls(multiplier=multiplier)
        session_id = snapshot.get("session_id")
        calc._session_id = session_id if session_id is not None else _UNSET
        calc._cum_volume = _load(snapshot.get("cum_volume")) or _ZERO
        calc._cum_weighted_price = _load(snapshot.get("cum_weighted_price")) or _ZERO
        calc._cum_weighted_price_sq = _load(snapshot.get("cum_weighted_price_sq")) or _ZERO
        return calc


def session_vwap_bands(
    bars: Iterable[BarLike],
    multiplier: Decimal = Decimal("2.0"),
) -> list[VWAPBands | None]:
    """Calculate point-in-time VWAP deviation bands for a sequence of bars."""
    calc = VWAPDeviationBandsCalculator(multiplier=multiplier)
    return [calc.update(bar) for bar in bars]
