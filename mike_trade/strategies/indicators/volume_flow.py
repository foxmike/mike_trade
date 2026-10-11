"""Persisted cumulative volume-flow indicators used by technical exit rules.

OBV and the Accumulation/Distribution Line are running totals.  Their absolute starting
value is immaterial, but restarting either calculation at the front of a bounded candle
window changes its apparent support levels.  These calculators therefore expose JSON-safe
snapshots for :func:`mike_trade.strategies.closed_bar.advance`.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from decimal import Decimal

from mike_trade.strategies.indicators.core import BarLike, _arithmetic, _decimal, _load, _period

__all__ = ["OnBalanceVolumeCalculator", "AccumulationDistributionCalculator"]

_ZERO = Decimal(0)
_TWO = Decimal(2)


class _SupportCalculator:
    """Common persisted rolling-support state for a cumulative line."""

    kind = ""

    def __init__(self, period: int = 20) -> None:
        self.period = _period(period)
        self.warmup_bars = self.period + 1
        self._value: Decimal | None = None
        self._history: deque[Decimal] = deque(maxlen=self.period)

    def _next(self, bar: BarLike) -> Decimal:
        raise NotImplementedError

    def update(self, bar: BarLike) -> tuple[Decimal, Decimal | None]:
        """Return ``(line, prior-period support)`` for the current completed bar."""
        value = self._next(bar)
        support = min(self._history) if len(self._history) == self.period else None
        self._history.append(value)
        return value, support

    def snapshot(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "period": self.period,
            "value": None if self._value is None else str(self._value),
            "history": [str(value) for value in self._history],
            **self._extra_snapshot(),
        }

    def _extra_snapshot(self) -> dict[str, object]:
        return {}

    @classmethod
    def _restore_common(cls, snapshot: Mapping[str, object]) -> _SupportCalculator:
        if snapshot.get("kind") != cls.kind:
            raise ValueError("indicator snapshot kind does not match")
        calc = cls(snapshot["period"])  # type: ignore[arg-type]
        calc._value = _load(snapshot["value"])
        history = snapshot["history"]
        if not isinstance(history, list) or len(history) > calc.period:
            raise ValueError("invalid cumulative indicator history")
        for text in history:
            calc._history.append(_decimal(Decimal(text)))
        return calc


class OnBalanceVolumeCalculator(_SupportCalculator):
    """Granville OBV plus the lowest OBV of the preceding ``period`` bars.

    The first value follows StockCharts' convention and equals the first bar's volume.
    Thereafter volume is added on an up close, subtracted on a down close, and unchanged
    on an equal close.  The support output excludes the current bar.
    """

    kind = "obv"

    def __init__(self, period: int = 20) -> None:
        super().__init__(period)
        self._close: Decimal | None = None

    def _next(self, bar: BarLike) -> Decimal:
        close, volume = _decimal(bar.close, "close"), _decimal(bar.volume, "volume")
        if volume < 0:
            raise ValueError("volume must not be negative")
        with _arithmetic():
            if self._value is None:
                self._value = volume
            elif self._close is not None and close > self._close:
                self._value += volume
            elif self._close is not None and close < self._close:
                self._value -= volume
        self._close = close
        return self._value

    def _extra_snapshot(self) -> dict[str, object]:
        return {"close": None if self._close is None else str(self._close)}

    @classmethod
    def restore(cls, snapshot: Mapping[str, object]) -> OnBalanceVolumeCalculator:
        calc = cls._restore_common(snapshot)
        assert isinstance(calc, cls)
        calc._close = _load(snapshot["close"])
        if (calc._value is None) != (calc._close is None):
            raise ValueError("inconsistent OBV snapshot")
        return calc


class AccumulationDistributionCalculator(_SupportCalculator):
    """Chaikin A/D Line plus the lowest A/D value of the preceding ``period`` bars.

    ``MFV = volume * ((close-low) - (high-close)) / (high-low)`` and the line is the
    running total of MFV.  A zero-range bar contributes zero.  Support excludes the
    current bar.
    """

    kind = "ad_line"

    def _next(self, bar: BarLike) -> Decimal:
        high = _decimal(bar.high, "high")
        low = _decimal(bar.low, "low")
        close = _decimal(bar.close, "close")
        volume = _decimal(bar.volume, "volume")
        if high < low or close < low or close > high:
            raise ValueError("bar must satisfy low <= close <= high")
        if volume < 0:
            raise ValueError("volume must not be negative")
        with _arithmetic():
            spread = high - low
            flow = _ZERO if spread == 0 else volume * ((_TWO * close - high - low) / spread)
            self._value = flow if self._value is None else self._value + flow
        return self._value

    @classmethod
    def restore(cls, snapshot: Mapping[str, object]) -> AccumulationDistributionCalculator:
        calc = cls._restore_common(snapshot)
        assert isinstance(calc, cls)
        return calc
