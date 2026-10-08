"""Pure Decimal technical indicators and incremental calculators.

All arithmetic runs in a fresh Decimal context with precision 28 and
ROUND_HALF_EVEN rounding. Inputs must be finite :class:`Decimal` values;
there is no dependence on the caller's Decimal context. RSI's flat-series
value is 50. EMA and ATR are unavailable until their documented seed is
complete. The module performs no I/O and keeps no shared mutable state.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from typing import Iterator, Protocol

__all__ = [
    "BarLike", "SessionBarLike", "EMACalculator", "RSICalculator",
    "ATRCalculator", "VWAPCalculator", "CumulativeSessionVolumeCalculator",
    "VolumeSMACalculator", "RelativeVolumeCalculator", "ema", "rsi", "atr",
    "session_vwap", "cumulative_session_volume", "volume_sma", "relative_volume",
]

_PRECISION = 28
_ZERO = Decimal("0")
_ONE = Decimal("1")
_TWO = Decimal("2")
_HUNDRED = Decimal("100")


@contextmanager
def _arithmetic() -> Iterator[None]:
    """Use the indicator arithmetic context, isolated from caller settings."""
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        ctx.rounding = ROUND_HALF_EVEN
        yield


def _decimal(value: object, field: str = "value") -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise TypeError(f"{field} must be a finite Decimal")
    return value


def _period(period: int) -> int:
    if isinstance(period, bool) or not isinstance(period, int) or period < 1:
        raise ValueError("period must be a positive int")
    return period


class BarLike(Protocol):
    """Minimal OHLCV shape accepted by candle-based calculators."""

    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


class SessionBarLike(BarLike, Protocol):
    """OHLCV bar carrying the caller-assigned session identifier."""

    session_id: object


class EMACalculator:
    def __init__(self, period: int) -> None:
        self.period = _period(period)
        self.warmup_bars = self.period
        self._seed: list[Decimal] = []
        self._value: Decimal | None = None
        with _arithmetic():
            self._alpha = _TWO / Decimal(self.period + 1)

    @property
    def ready(self) -> bool:
        return self._value is not None

    def update(self, value: Decimal) -> Decimal | None:
        value = _decimal(value)
        if self._value is None:
            self._seed.append(value)
            if len(self._seed) < self.period:
                return None
            with _arithmetic():
                self._value = sum(self._seed, _ZERO) / Decimal(self.period)
            self._seed.clear()
            return self._value
        with _arithmetic():
            self._value = self._alpha * value + (_ONE - self._alpha) * self._value
        return self._value


class RSICalculator:
    """Wilder RSI; a series with no gains and no losses returns exactly 50."""

    def __init__(self, period: int) -> None:
        self.period = _period(period)
        self.warmup_bars = self.period + 1
        self._previous: Decimal | None = None
        self._changes: list[tuple[Decimal, Decimal]] = []
        self._avg_gain: Decimal | None = None
        self._avg_loss: Decimal | None = None

    @property
    def ready(self) -> bool:
        return self._avg_gain is not None

    def _result(self) -> Decimal:
        assert self._avg_gain is not None and self._avg_loss is not None
        if self._avg_loss == 0:
            return _HUNDRED if self._avg_gain > 0 else Decimal("50")
        with _arithmetic():
            result = _HUNDRED - _HUNDRED / (_ONE + self._avg_gain / self._avg_loss)
        return min(_HUNDRED, max(_ZERO, result))

    def update(self, close: Decimal) -> Decimal | None:
        close = _decimal(close, "close")
        if self._previous is None:
            self._previous = close
            return None
        with _arithmetic():
            delta = close - self._previous
            gain, loss = max(delta, _ZERO), max(-delta, _ZERO)
        self._previous = close
        if self._avg_gain is None:
            self._changes.append((gain, loss))
            if len(self._changes) < self.period:
                return None
            with _arithmetic():
                self._avg_gain = sum((g for g, _ in self._changes), _ZERO) / Decimal(self.period)
                self._avg_loss = sum((l for _, l in self._changes), _ZERO) / Decimal(self.period)
            self._changes.clear()
        else:
            with _arithmetic():
                p = Decimal(self.period)
                self._avg_gain = (self._avg_gain * (p - _ONE) + gain) / p
                self._avg_loss = (self._avg_loss * (p - _ONE) + loss) / p
        return self._result()


class ATRCalculator:
    def __init__(self, period: int) -> None:
        self.period = _period(period)
        self.warmup_bars = self.period
        self._previous_close: Decimal | None = None
        self._seed: list[Decimal] = []
        self._value: Decimal | None = None

    @property
    def ready(self) -> bool:
        return self._value is not None

    def update(self, bar: BarLike) -> Decimal | None:
        high, low, close = (_decimal(getattr(bar, name), name) for name in ("high", "low", "close"))
        if high < low:
            raise ValueError("high must be >= low")
        if self._previous_close is None:
            with _arithmetic():
                tr = high - low
        else:
            with _arithmetic():
                tr = max(high - low, abs(high - self._previous_close), abs(low - self._previous_close))
        self._previous_close = close
        if self._value is None:
            self._seed.append(tr)
            if len(self._seed) < self.period:
                return None
            with _arithmetic():
                self._value = sum(self._seed, _ZERO) / Decimal(self.period)
            self._seed.clear()
            return self._value
        with _arithmetic():
            p = Decimal(self.period)
            self._value = (self._value * (p - _ONE) + tr) / p
        return self._value


class VWAPCalculator:
    def __init__(self) -> None:
        self.warmup_bars = 1
        self._session_id: object = _UNSET
        self._weighted_volume = _ZERO
        self._volume = _ZERO

    @property
    def ready(self) -> bool:
        return self._volume > 0

    def update(self, bar: SessionBarLike) -> Decimal | None:
        session_id = getattr(bar, "session_id")
        if self._session_id is _UNSET or session_id != self._session_id:
            self._session_id = session_id
            self._weighted_volume = _ZERO
            self._volume = _ZERO
        high, low, close, volume = (_decimal(getattr(bar, name), name) for name in ("high", "low", "close", "volume"))
        if volume < 0:
            raise ValueError("volume must be >= 0")
        if volume > 0:
            with _arithmetic():
                typical = (high + low + close) / Decimal(3)
                self._weighted_volume += typical * volume
                self._volume += volume
        if self._volume == 0:
            return None
        with _arithmetic():
            return self._weighted_volume / self._volume


_UNSET = object()


class CumulativeSessionVolumeCalculator:
    def __init__(self) -> None:
        self.warmup_bars = 1
        self._session_id: object = _UNSET
        self._volume = _ZERO

    @property
    def ready(self) -> bool:
        return self._session_id is not _UNSET

    def update(self, volume: Decimal, session_id: object) -> Decimal:
        volume = _decimal(volume, "volume")
        if volume < 0:
            raise ValueError("volume must be >= 0")
        if self._session_id is _UNSET or session_id != self._session_id:
            self._session_id, self._volume = session_id, _ZERO
        with _arithmetic():
            self._volume += volume
        return self._volume


class VolumeSMACalculator:
    def __init__(self, period: int) -> None:
        self.period = _period(period)
        self.warmup_bars = self.period
        self._values: deque[Decimal] = deque(maxlen=self.period)

    @property
    def ready(self) -> bool:
        return len(self._values) == self.period

    def update(self, volume: Decimal) -> Decimal | None:
        volume = _decimal(volume, "volume")
        if volume < 0:
            raise ValueError("volume must be >= 0")
        self._values.append(volume)
        if not self.ready:
            return None
        with _arithmetic():
            return sum(self._values, _ZERO) / Decimal(self.period)


class RelativeVolumeCalculator:
    def __init__(self, period: int) -> None:
        self.period = _period(period)
        self.warmup_bars = self.period
        self._sma = VolumeSMACalculator(period)

    @property
    def ready(self) -> bool:
        return self._sma.ready

    def update(self, volume: Decimal) -> Decimal | None:
        volume = _decimal(volume, "volume")
        average = self._sma.update(volume)
        if average is None or average == 0:
            return None
        with _arithmetic():
            return volume / average


def ema(values: Iterable[Decimal], period: int) -> list[Decimal | None]:
    calc = EMACalculator(period)
    return [calc.update(value) for value in values]


def rsi(closes: Iterable[Decimal], period: int) -> list[Decimal | None]:
    calc = RSICalculator(period)
    return [calc.update(close) for close in closes]


def atr(bars: Iterable[BarLike], period: int) -> list[Decimal | None]:
    calc = ATRCalculator(period)
    return [calc.update(bar) for bar in bars]


def session_vwap(bars: Iterable[SessionBarLike]) -> list[Decimal | None]:
    calc = VWAPCalculator()
    return [calc.update(bar) for bar in bars]


def cumulative_session_volume(volumes: Iterable[tuple[Decimal, object]]) -> list[Decimal]:
    calc = CumulativeSessionVolumeCalculator()
    return [calc.update(volume, session_id) for volume, session_id in volumes]


def volume_sma(volumes: Iterable[Decimal], period: int) -> list[Decimal | None]:
    calc = VolumeSMACalculator(period)
    return [calc.update(volume) for volume in volumes]


def relative_volume(volumes: Iterable[Decimal], period: int) -> list[Decimal | None]:
    calc = RelativeVolumeCalculator(period)
    return [calc.update(volume) for volume in volumes]
