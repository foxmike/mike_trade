"""Pure Decimal technical indicators and incremental calculators.

All arithmetic runs in a fresh Decimal context with precision 28 and
ROUND_HALF_EVEN rounding. Inputs must be finite :class:`Decimal` values;
there is no dependence on the caller's Decimal context. RSI's flat-series
value is 50. EMA and ATR are unavailable until their documented seed is
complete. The series functions at the end of the module (``sma`` ... ``parabolic_sar``)
take a whole oldest-first history and return lists aligned with the input, with ``None``
wherever a value is not yet defined (or is mathematically undefined, such as a flat
range); element ``i`` depends only on inputs ``0..i``, never on later inputs. The recursive
calculators (EMA, RSI, MACD, DMI, Parabolic SAR) can ``snapshot()`` their state into plain
JSON-safe values (Decimals as strings) and ``restore()`` it, so a strategy can resume them
across evaluations and restarts instead of re-deriving the seed from a bounded history
window. The module performs no I/O and keeps no shared mutable state.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from typing import Iterator, Protocol

__all__ = [
    "BarLike", "SessionBarLike", "EMACalculator", "RSICalculator",
    "ATRCalculator", "VWAPCalculator", "CumulativeSessionVolumeCalculator",
    "VolumeSMACalculator", "RelativeVolumeCalculator", "ema", "rsi", "atr",
    "session_vwap", "cumulative_session_volume", "volume_sma", "relative_volume",
    "MACDCalculator", "DMICalculator", "ParabolicSARCalculator",
    "sma", "macd", "stochastic", "williams_r", "cci", "donchian",
    "directional_indicators", "aroon", "parabolic_sar",
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


def _dump(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _load(value: object) -> Decimal | None:
    """Inverse of :func:`_dump`; rejects anything that is not a finite Decimal string."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("snapshot decimals must be strings")
    return _decimal(Decimal(value))


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

    def snapshot(self) -> dict[str, object]:
        return {"period": self.period, "seed": [str(v) for v in self._seed],
                "value": _dump(self._value)}

    @classmethod
    def restore(cls, snapshot: Mapping[str, object]) -> EMACalculator:
        calc = cls(snapshot["period"])  # type: ignore[arg-type]
        calc._seed = [_decimal(Decimal(v)) for v in snapshot["seed"]]  # type: ignore[union-attr]
        calc._value = _load(snapshot["value"])
        if calc._value is None and len(calc._seed) >= calc.period:
            raise ValueError("inconsistent EMA snapshot")
        return calc


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

    def snapshot(self) -> dict[str, object]:
        return {
            "period": self.period, "previous": _dump(self._previous),
            "changes": [[str(g), str(l)] for g, l in self._changes],
            "avg_gain": _dump(self._avg_gain), "avg_loss": _dump(self._avg_loss),
        }

    @classmethod
    def restore(cls, snapshot: Mapping[str, object]) -> RSICalculator:
        calc = cls(snapshot["period"])  # type: ignore[arg-type]
        calc._previous = _load(snapshot["previous"])
        calc._changes = [
            (_decimal(Decimal(g)), _decimal(Decimal(l)))
            for g, l in snapshot["changes"]  # type: ignore[union-attr]
        ]
        calc._avg_gain, calc._avg_loss = _load(snapshot["avg_gain"]), _load(snapshot["avg_loss"])
        if (calc._avg_gain is None) != (calc._avg_loss is None):
            raise ValueError("inconsistent RSI snapshot")
        return calc


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


# ---------------------------------------------------------------------- series functions
#
# Whole-history, oldest-first calculators used by the technical strategies. Each runs
# in one ``_arithmetic()`` context (cheap enough to recompute per evaluation) and each
# element depends only on bars at or before its own index.


class _HLC(Protocol):
    high: Decimal
    low: Decimal
    close: Decimal


def _series(values: Iterable[Decimal], name: str = "value") -> list[Decimal]:
    return [_decimal(v, name) for v in values]


def _sma_list(values: Sequence[Decimal | None], period: int) -> list[Decimal | None]:
    """SMA over ``values``; ``None`` if any value inside the window is ``None``."""
    out: list[Decimal | None] = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        if any(v is None for v in window):
            continue
        out[i] = sum(window, _ZERO) / Decimal(period)  # type: ignore[arg-type]
    return out


def sma(values: Iterable[Decimal], period: int) -> list[Decimal | None]:
    """Simple moving average; first value at index ``period - 1``."""
    period = _period(period)
    with _arithmetic():
        return _sma_list(_series(values), period)


class MACDCalculator:
    """Appel MACD from closes; ``update`` returns ``(fast_ema, slow_ema, macd, signal)``.

    ``macd = EMA(fast) - EMA(slow)``; ``signal`` is the EMA of the MACD line seeded with
    the SMA of its first ``signal`` values (``None`` when ``signal`` is ``None``). Each
    element is ``None`` until its seed is complete.
    """

    def __init__(self, fast: int = 12, slow: int = 26, signal: int | None = 9) -> None:
        self.fast_period, self.slow_period = _period(fast), _period(slow)
        if self.fast_period >= self.slow_period:
            raise ValueError("fast period must be shorter than slow period")
        self.signal_period = None if signal is None else _period(signal)
        self._fast, self._slow = EMACalculator(self.fast_period), EMACalculator(self.slow_period)
        self._signal = None if signal is None else EMACalculator(signal)
        self.warmup_bars = self.slow_period + (self.signal_period or 1) - 1

    def update(
        self, close: Decimal
    ) -> tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None]:
        fast, slow = self._fast.update(close), self._slow.update(close)
        line: Decimal | None = None
        if fast is not None and slow is not None:
            with _arithmetic():
                line = fast - slow
        signal = None
        if self._signal is not None and line is not None:
            signal = self._signal.update(line)
        return fast, slow, line, signal

    def snapshot(self) -> dict[str, object]:
        return {
            "fast": self._fast.snapshot(), "slow": self._slow.snapshot(),
            "signal": None if self._signal is None else self._signal.snapshot(),
        }

    @classmethod
    def restore(cls, snapshot: Mapping[str, object]) -> MACDCalculator:
        fast, slow = snapshot["fast"], snapshot["slow"]
        signal = snapshot["signal"]
        calc = cls(
            fast["period"], slow["period"],  # type: ignore[index]
            None if signal is None else signal["period"],  # type: ignore[index]
        )
        calc._fast = EMACalculator.restore(fast)  # type: ignore[arg-type]
        calc._slow = EMACalculator.restore(slow)  # type: ignore[arg-type]
        if signal is not None:
            calc._signal = EMACalculator.restore(signal)  # type: ignore[arg-type]
        else:
            calc._signal = None
        return calc


def macd(
    closes: Iterable[Decimal], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[list[Decimal | None], list[Decimal | None], list[Decimal | None]]:
    """Whole-history MACD ``(macd_line, signal_line, histogram)`` via :class:`MACDCalculator`."""
    calc = MACDCalculator(fast, slow, signal)
    line: list[Decimal | None] = []
    sig: list[Decimal | None] = []
    for close in closes:
        _, _, m, g = calc.update(close)
        line.append(m)
        sig.append(g)
    with _arithmetic():
        hist = [None if m is None or g is None else m - g for m, g in zip(line, sig)]
    return line, sig, hist


def _window_extremes(
    bars: Sequence[_HLC], period: int
) -> list[tuple[Decimal, Decimal] | None]:
    """``(highest high, lowest low)`` over the ``period`` bars ending at each index."""
    out: list[tuple[Decimal, Decimal] | None] = [None] * len(bars)
    for i in range(period - 1, len(bars)):
        window = bars[i - period + 1 : i + 1]
        out[i] = (max(b.high for b in window), min(b.low for b in window))
    return out


def stochastic(
    bars: Iterable[_HLC], k_period: int = 14, k_smooth: int = 3, d_period: int = 3
) -> tuple[list[Decimal | None], list[Decimal | None]]:
    """Lane Stochastic ``(%K, %D)`` as in StockCharts' Full Stochastic (14, 3, 3).

    Raw %K = 100 * (close - lowest low) / (highest high - lowest low) over ``k_period``
    bars; %K = SMA(raw %K, ``k_smooth``); %D = SMA(%K, ``d_period``). ``k_smooth=1`` gives
    the Fast Stochastic. A flat window (high == low) leaves raw %K undefined (``None``).
    """
    k_period, k_smooth, d_period = _period(k_period), _period(k_smooth), _period(d_period)
    data = list(bars)
    for b in data:
        _decimal(b.high, "high"), _decimal(b.low, "low"), _decimal(b.close, "close")
    with _arithmetic():
        extremes = _window_extremes(data, k_period)
        raw: list[Decimal | None] = []
        for bar, ext in zip(data, extremes):
            if ext is None or ext[0] == ext[1]:
                raw.append(None)
            else:
                raw.append(_HUNDRED * (bar.close - ext[1]) / (ext[0] - ext[1]))
        k = _sma_list(raw, k_smooth)
        d = _sma_list(k, d_period)
    return k, d


def williams_r(bars: Iterable[_HLC], period: int = 14) -> list[Decimal | None]:
    """Williams %R = -100 * (highest high - close) / (highest high - lowest low)."""
    period = _period(period)
    data = list(bars)
    for b in data:
        _decimal(b.high, "high"), _decimal(b.low, "low"), _decimal(b.close, "close")
    with _arithmetic():
        out: list[Decimal | None] = []
        for bar, ext in zip(data, _window_extremes(data, period)):
            if ext is None or ext[0] == ext[1]:
                out.append(None)
            else:
                out.append(-_HUNDRED * (ext[0] - bar.close) / (ext[0] - ext[1]))
    return out


def cci(bars: Iterable[_HLC], period: int = 20) -> list[Decimal | None]:
    """Lambert CCI = (TP - SMA(TP)) / (0.015 * mean deviation), TP = (H + L + C) / 3."""
    period = _period(period)
    data = list(bars)
    for b in data:
        _decimal(b.high, "high"), _decimal(b.low, "low"), _decimal(b.close, "close")
    out: list[Decimal | None] = [None] * len(data)
    with _arithmetic():
        typical = [(b.high + b.low + b.close) / Decimal(3) for b in data]
        constant = Decimal("0.015")
        count = Decimal(period)
        for i in range(period - 1, len(data)):
            window = typical[i - period + 1 : i + 1]
            mean = sum(window, _ZERO) / count
            deviation = sum((abs(v - mean) for v in window), _ZERO) / count
            if deviation == 0:
                continue
            out[i] = (typical[i] - mean) / (constant * deviation)
    return out


def donchian(
    bars: Iterable[_HLC], period: int = 20
) -> tuple[list[Decimal | None], list[Decimal | None]]:
    """Donchian channel ``(upper, lower)`` from the ``period`` bars *before* each bar.

    Upper = highest high and lower = lowest low of the preceding ``period`` bars; the
    current bar is excluded (StockCharts: price channels use "prices prior to the current
    period"), so a close above ``upper[i]`` is a genuine new ``period``-bar high.
    """
    period = _period(period)
    data = list(bars)
    for b in data:
        _decimal(b.high, "high"), _decimal(b.low, "low")
    upper: list[Decimal | None] = [None] * len(data)
    lower: list[Decimal | None] = [None] * len(data)
    for i in range(period, len(data)):
        window = data[i - period : i]
        upper[i] = max(b.high for b in window)
        lower[i] = min(b.low for b in window)
    return upper, lower


class DMICalculator:
    """Wilder +DI/-DI with StockCharts' smoothing; ``update(bar)`` returns ``(+DI, -DI)``.

    TR, +DM and -DM start at the second bar. The first smoothed value is the sum of the
    first ``period`` raw values; afterwards ``smoothed = previous - previous / period +
    current``. ``(None, None)`` until ``period`` raw values exist or while smoothed TR is 0.
    """

    def __init__(self, period: int = 14) -> None:
        self.period = _period(period)
        self.warmup_bars = self.period + 1
        self._prev: tuple[Decimal, Decimal, Decimal] | None = None
        self._count = 0
        self._tr = self._plus = self._minus = _ZERO

    def update(self, bar: _HLC) -> tuple[Decimal | None, Decimal | None]:
        high, low, close = (_decimal(getattr(bar, n), n) for n in ("high", "low", "close"))
        prev, self._prev = self._prev, (high, low, close)
        if prev is None:
            return None, None
        prev_high, prev_low, prev_close = prev
        with _arithmetic():
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            up, down = high - prev_high, prev_low - low
            dm_plus = up if up > down and up > 0 else _ZERO
            dm_minus = down if down > up and down > 0 else _ZERO
            self._count += 1
            if self._count <= self.period:
                self._tr, self._plus, self._minus = (
                    self._tr + tr, self._plus + dm_plus, self._minus + dm_minus)
                if self._count < self.period:
                    return None, None
            else:
                count = Decimal(self.period)
                self._tr += tr - self._tr / count
                self._plus += dm_plus - self._plus / count
                self._minus += dm_minus - self._minus / count
            if self._tr <= 0:
                return None, None
            return _HUNDRED * self._plus / self._tr, _HUNDRED * self._minus / self._tr

    def snapshot(self) -> dict[str, object]:
        return {
            "period": self.period, "count": self._count,
            "prev": None if self._prev is None else [str(v) for v in self._prev],
            "tr": str(self._tr), "plus": str(self._plus), "minus": str(self._minus),
        }

    @classmethod
    def restore(cls, snapshot: Mapping[str, object]) -> DMICalculator:
        calc = cls(snapshot["period"])  # type: ignore[arg-type]
        prev = snapshot["prev"]
        if prev is not None:
            high, low, close = (_decimal(Decimal(v)) for v in prev)  # type: ignore[union-attr]
            calc._prev = (high, low, close)
        calc._count = int(snapshot["count"])  # type: ignore[call-overload]
        tr, plus, minus = (
            _load(snapshot[k]) for k in ("tr", "plus", "minus")
        )
        if tr is None or plus is None or minus is None:
            raise ValueError("inconsistent DMI snapshot")
        calc._tr, calc._plus, calc._minus = tr, plus, minus
        return calc


def directional_indicators(
    bars: Iterable[_HLC], period: int = 14
) -> tuple[list[Decimal | None], list[Decimal | None]]:
    """Whole-history Wilder ``(+DI, -DI)`` via :class:`DMICalculator`."""
    calc = DMICalculator(period)
    plus: list[Decimal | None] = []
    minus: list[Decimal | None] = []
    for bar in bars:
        p, m = calc.update(bar)
        plus.append(p)
        minus.append(m)
    return plus, minus


def aroon(
    bars: Iterable[_HLC], period: int = 25
) -> tuple[list[Decimal | None], list[Decimal | None]]:
    """Chande Aroon ``(up, down)``: ``100 * (period - bars since extreme) / period``.

    The extreme is searched over the latest ``period + 1`` bars (the current bar plus the
    ``period`` before it); ties resolve to the most recent bar. First defined at index
    ``period``.
    """
    period = _period(period)
    data = list(bars)
    for b in data:
        _decimal(b.high, "high"), _decimal(b.low, "low")
    up: list[Decimal | None] = [None] * len(data)
    down: list[Decimal | None] = [None] * len(data)
    with _arithmetic():
        count = Decimal(period)
        for i in range(period, len(data)):
            window = data[i - period : i + 1]
            since_high = since_low = 0
            high, low = window[0].high, window[0].low
            for offset, bar in enumerate(window):
                if bar.high >= high:
                    high, since_high = bar.high, period - offset
                if bar.low <= low:
                    low, since_low = bar.low, period - offset
            up[i] = _HUNDRED * (count - Decimal(since_high)) / count
            down[i] = _HUNDRED * (count - Decimal(since_low)) / count
    return up, down


class ParabolicSARCalculator:
    """Wilder Parabolic SAR; ``update(bar)`` returns ``(sar, is_uptrend)`` (``None`` for bar 1).

    ``SAR = prior SAR + AF * (EP - prior SAR)``; AF starts at ``step``, grows by ``step`` on
    each new extreme point and is capped at ``maximum``. In an uptrend SAR is never above
    the prior two bars' lows (in a downtrend never below their highs). When the bar
    penetrates SAR (low < SAR in an uptrend, high > SAR in a downtrend) the trend reverses:
    the new SAR is the prior EP, the new EP is the penetrating bar's extreme and AF resets.
    Initial trend (an interpretation, see docs): uptrend if the second close is at or above
    the first, else downtrend; the first SAR is the first bar's low (high), EP its high (low).
    """

    def __init__(self, step: Decimal = Decimal("0.02"), maximum: Decimal = Decimal("0.20")) -> None:
        self.step, self.maximum = _decimal(step, "step"), _decimal(maximum, "maximum")
        if self.step <= 0 or self.maximum < self.step:
            raise ValueError("step must be positive and maximum must be >= step")
        self.warmup_bars = 2
        self._first_close: Decimal | None = None
        self._recent: list[tuple[Decimal, Decimal]] = []  # (high, low) of the last two bars
        self._up = True
        self._sar = self._ep = self._af = _ZERO
        self._started = False

    def update(self, bar: _HLC) -> tuple[Decimal, bool] | None:
        high, low, close = (_decimal(getattr(bar, n), n) for n in ("high", "low", "close"))
        if self._first_close is None:
            self._first_close = close
            self._recent = [(high, low)]
            return None
        with _arithmetic():
            if not self._started:
                self._started = True
                self._up = close >= self._first_close
                first_high, first_low = self._recent[0]
                self._sar = first_low if self._up else first_high
                self._ep = first_high if self._up else first_low
                self._af = self.step
            sar = self._sar + self._af * (self._ep - self._sar)
            if self._up:
                sar = min(sar, *(lo for _, lo in self._recent))
                if low < sar:
                    self._up, sar, self._ep, self._af = False, self._ep, low, self.step
                elif high > self._ep:
                    self._ep, self._af = high, min(self._af + self.step, self.maximum)
            else:
                sar = max(sar, *(hi for hi, _ in self._recent))
                if high > sar:
                    self._up, sar, self._ep, self._af = True, self._ep, high, self.step
                elif low < self._ep:
                    self._ep, self._af = low, min(self._af + self.step, self.maximum)
            self._sar = sar
        self._recent = [*self._recent, (high, low)][-2:]
        return sar, self._up

    def snapshot(self) -> dict[str, object]:
        return {
            "step": str(self.step), "maximum": str(self.maximum),
            "first_close": _dump(self._first_close), "started": self._started,
            "recent": [[str(h), str(lo)] for h, lo in self._recent], "up": self._up,
            "sar": str(self._sar), "ep": str(self._ep), "af": str(self._af),
        }

    @classmethod
    def restore(cls, snapshot: Mapping[str, object]) -> ParabolicSARCalculator:
        step, maximum = _load(snapshot["step"]), _load(snapshot["maximum"])
        if step is None or maximum is None:
            raise ValueError("inconsistent Parabolic SAR snapshot")
        calc = cls(step, maximum)
        calc._first_close = _load(snapshot["first_close"])
        calc._started = bool(snapshot["started"])
        calc._recent = [
            (_decimal(Decimal(h)), _decimal(Decimal(lo)))
            for h, lo in snapshot["recent"]  # type: ignore[union-attr]
        ]
        calc._up = bool(snapshot["up"])
        calc._sar, calc._ep, calc._af = (
            _decimal(Decimal(snapshot[k])) for k in ("sar", "ep", "af")  # type: ignore[arg-type]
        )
        return calc


def parabolic_sar(
    bars: Iterable[_HLC],
    step: Decimal = Decimal("0.02"),
    maximum: Decimal = Decimal("0.20"),
) -> list[tuple[Decimal, bool] | None]:
    """Whole-history Parabolic SAR ``(sar, is_uptrend)`` via :class:`ParabolicSARCalculator`."""
    calc = ParabolicSARCalculator(step, maximum)
    return [calc.update(bar) for bar in bars]
