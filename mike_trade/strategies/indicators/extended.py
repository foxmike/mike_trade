"""Resumable recursive indicators used by the third BUY batch.

Each calculator follows the contract of :mod:`mike_trade.strategies.indicators.core`: Decimal
arithmetic in the shared indicator context, ``update`` returning a tuple of Decimals / bools /
``None`` (``None`` until the published seed is complete), and JSON-safe ``snapshot()`` /
``restore()`` so a strategy can resume it with :func:`mike_trade.strategies.closed_bar.advance`
instead of re-deriving the seed from a bounded candle window.  Nothing here pads or fabricates
observations: a calculator only ever sees the bars it is fed.

This module adds calculators; it does not change the shared ones it builds on.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from decimal import Decimal

from mike_trade.strategies.indicators.core import (
    ATRCalculator,
    BarLike,
    DMICalculator,
    EMACalculator,
    MACDCalculator,
    RSICalculator,
    _HLC,
    _arithmetic,
    _decimal,
    _dump,
    _load,
    _period,
)

__all__ = [
    "SnapshotATRCalculator",
    "ADXCalculator",
    "SupertrendCalculator",
    "KAMACalculator",
    "EMARibbonCalculator",
    "KeltnerChannelCalculator",
    "ConnorsRSICalculator",
    "ElderImpulseCalculator",
]

_ZERO = Decimal("0")
_ONE = Decimal("1")
_TWO = Decimal("2")
_HUNDRED = Decimal("100")


def _required(snapshot: Mapping[str, object], key: str) -> Decimal:
    value = _load(snapshot[key])
    if value is None:
        raise ValueError(f"inconsistent snapshot: {key} is missing")
    return value


# ------------------------------------------------------------------------------ ATR


class SnapshotATRCalculator(ATRCalculator):
    """Wilder ATR (SMA seed of the first ``period`` true ranges) with snapshot/restore.

    Identical arithmetic to :class:`ATRCalculator`; only persistence is added.
    """

    def snapshot(self) -> dict[str, object]:
        return {
            "period": self.period, "previous_close": _dump(self._previous_close),
            "seed": [str(v) for v in self._seed], "value": _dump(self._value),
        }

    @classmethod
    def restore(cls, snapshot: Mapping[str, object]) -> SnapshotATRCalculator:
        calc = cls(snapshot["period"])  # type: ignore[arg-type]
        calc._previous_close = _load(snapshot["previous_close"])
        calc._seed = [_decimal(Decimal(v)) for v in snapshot["seed"]]  # type: ignore[union-attr]
        calc._value = _load(snapshot["value"])
        if calc._value is None and len(calc._seed) >= calc.period:
            raise ValueError("inconsistent ATR snapshot")
        return calc


# ------------------------------------------------------------------------------ ADX


class ADXCalculator:
    """Wilder ADX; ``update(bar)`` returns ``(+DI, -DI, ADX)``.

    +DI/-DI come from :class:`DMICalculator` (first defined on bar ``period + 1``).
    ``DX = 100 * |+DI - -DI| / (+DI + -DI)`` (0 when both are 0).  The first ADX is the mean of
    the first ``period`` DX values (bar ``2 * period``); afterwards
    ``ADX = (previous * (period - 1) + DX) / period`` (StockCharts ChartSchool, ADX).  When the
    smoothed true range is 0 (a perfectly flat window) no directional movement exists: the DIs
    are ``None`` and that bar's DX is 0.
    """

    def __init__(self, period: int = 14) -> None:
        self.period = _period(period)
        self.warmup_bars = 2 * self.period
        self._dmi = DMICalculator(self.period)
        self._bars = 0
        self._dx_seed: list[Decimal] = []
        self._adx: Decimal | None = None

    def update(self, bar: _HLC) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
        plus, minus = self._dmi.update(bar)
        self._bars += 1
        if self._bars <= self.period:
            return None, None, None
        with _arithmetic():
            total = _ZERO if plus is None or minus is None else plus + minus
            dx = _ZERO if total == 0 else _HUNDRED * abs(plus - minus) / total  # type: ignore[operator]
            if self._adx is None:
                self._dx_seed.append(dx)
                if len(self._dx_seed) < self.period:
                    return plus, minus, None
                self._adx = sum(self._dx_seed, _ZERO) / Decimal(self.period)
                self._dx_seed.clear()
            else:
                p = Decimal(self.period)
                self._adx = (self._adx * (p - _ONE) + dx) / p
        return plus, minus, self._adx

    def snapshot(self) -> dict[str, object]:
        return {
            "dmi": self._dmi.snapshot(), "bars": self._bars,
            "dx_seed": [str(v) for v in self._dx_seed], "adx": _dump(self._adx),
        }

    @classmethod
    def restore(cls, snapshot: Mapping[str, object]) -> ADXCalculator:
        dmi = snapshot["dmi"]
        calc = cls(dmi["period"])  # type: ignore[index]
        calc._dmi = DMICalculator.restore(dmi)  # type: ignore[arg-type]
        calc._bars = int(snapshot["bars"])  # type: ignore[call-overload]
        calc._dx_seed = [_decimal(Decimal(v)) for v in snapshot["dx_seed"]]  # type: ignore[union-attr]
        calc._adx = _load(snapshot["adx"])
        if calc._adx is None and len(calc._dx_seed) >= calc.period:
            raise ValueError("inconsistent ADX snapshot")
        return calc


# ------------------------------------------------------------------------ Supertrend


class SupertrendCalculator:
    """Olivier Seban's Supertrend; ``update(bar)`` returns ``(supertrend, is_uptrend)``.

    ``basic bands = (high + low) / 2 +/- multiplier * ATR`` with Wilder ATR (SMA seed).  The
    final upper band only moves down unless the prior close was above it; the final lower band
    only moves up unless the prior close was below it.  In a downtrend the Supertrend is the
    final upper band and the trend turns up when the close is above it; in an uptrend it is
    the final lower band and the trend turns down when the close is below it.  Following the
    TradingView ``ta.supertrend`` reference, the first bar with an ATR starts in a downtrend
    (an interpretation; the initial direction is not part of the published definition).
    """

    def __init__(self, atr_period: int = 10, multiplier: Decimal = Decimal(3)) -> None:
        self.multiplier = _decimal(multiplier, "multiplier")
        if self.multiplier <= 0:
            raise ValueError("multiplier must be positive")
        self._atr = SnapshotATRCalculator(atr_period)
        self.atr_period = self._atr.period
        self.warmup_bars = self.atr_period
        self._upper: Decimal | None = None
        self._lower: Decimal | None = None
        self._close: Decimal | None = None
        self._up = False

    def update(self, bar: BarLike) -> tuple[Decimal, bool] | None:
        close = _decimal(bar.close, "close")
        atr = self._atr.update(bar)
        previous_close, self._close = self._close, close
        if atr is None:
            return None
        with _arithmetic():
            mid = (bar.high + bar.low) / _TWO
            upper, lower = mid + self.multiplier * atr, mid - self.multiplier * atr
            if self._upper is None or self._lower is None or previous_close is None:
                self._upper, self._lower, self._up = upper, lower, False
            else:
                prev_upper, prev_lower = self._upper, self._lower
                self._upper = upper if upper < prev_upper or previous_close > prev_upper else prev_upper
                self._lower = lower if lower > prev_lower or previous_close < prev_lower else prev_lower
                if self._up:
                    self._up = not close < self._lower
                else:
                    self._up = close > self._upper
        return (self._lower if self._up else self._upper), self._up

    def snapshot(self) -> dict[str, object]:
        return {
            "atr": self._atr.snapshot(), "multiplier": str(self.multiplier),
            "upper": _dump(self._upper), "lower": _dump(self._lower),
            "close": _dump(self._close), "up": self._up,
        }

    @classmethod
    def restore(cls, snapshot: Mapping[str, object]) -> SupertrendCalculator:
        atr = snapshot["atr"]
        calc = cls(atr["period"], _required(snapshot, "multiplier"))  # type: ignore[index]
        calc._atr = SnapshotATRCalculator.restore(atr)  # type: ignore[arg-type]
        calc._upper, calc._lower = _load(snapshot["upper"]), _load(snapshot["lower"])
        calc._close, calc._up = _load(snapshot["close"]), bool(snapshot["up"])
        if (calc._upper is None) != (calc._lower is None):
            raise ValueError("inconsistent Supertrend snapshot")
        return calc


# ----------------------------------------------------------------------------- KAMA


class KAMACalculator:
    """Perry Kaufman's Adaptive Moving Average; ``update(close)`` returns KAMA.

    ``ER = |close - close[n]| / sum(|close[i] - close[i-1]|, n)`` (0 for a flat window),
    ``SC = (ER * (2/(fast+1) - 2/(slow+1)) + 2/(slow+1)) ** 2`` and
    ``KAMA = KAMA[-1] + SC * (close - KAMA[-1])``.  The seed (StockCharts ChartSchool, KAMA:
    "the first KAMA is a simple moving average") is the SMA of the first ``er_period`` closes;
    the recursion starts on the next close.
    """

    def __init__(self, er_period: int = 10, fast: int = 2, slow: int = 30) -> None:
        self.er_period, self.fast, self.slow = _period(er_period), _period(fast), _period(slow)
        if self.fast >= self.slow:
            raise ValueError("fast must be shorter than slow")
        self.warmup_bars = self.er_period
        self._closes: deque[Decimal] = deque(maxlen=self.er_period + 1)
        self._value: Decimal | None = None

    def update(self, close: Decimal) -> Decimal | None:
        close = _decimal(close, "close")
        self._closes.append(close)
        with _arithmetic():
            if self._value is None:
                if len(self._closes) < self.er_period:
                    return None
                self._value = sum(self._closes, _ZERO) / Decimal(self.er_period)
                return self._value
            values = list(self._closes)
            change = abs(values[-1] - values[0])
            volatility = sum((abs(b - a) for a, b in zip(values, values[1:])), _ZERO)
            ratio = _ZERO if volatility == 0 else change / volatility
            fast_sc, slow_sc = _TWO / Decimal(self.fast + 1), _TWO / Decimal(self.slow + 1)
            smoothing = (ratio * (fast_sc - slow_sc) + slow_sc) ** 2
            self._value = self._value + smoothing * (close - self._value)
        return self._value

    def snapshot(self) -> dict[str, object]:
        return {
            "er_period": self.er_period, "fast": self.fast, "slow": self.slow,
            "closes": [str(v) for v in self._closes], "value": _dump(self._value),
        }

    @classmethod
    def restore(cls, snapshot: Mapping[str, object]) -> KAMACalculator:
        calc = cls(snapshot["er_period"], snapshot["fast"], snapshot["slow"])  # type: ignore[arg-type]
        for text in snapshot["closes"]:  # type: ignore[union-attr]
            calc._closes.append(_decimal(Decimal(text)))
        calc._value = _load(snapshot["value"])
        if calc._value is None and len(calc._closes) >= calc.er_period:
            raise ValueError("inconsistent KAMA snapshot")
        return calc


# ----------------------------------------------------------------------- EMA ribbon


class EMARibbonCalculator:
    """A group of SMA-seeded EMAs; ``update(close)`` returns ``(lowest_short, highest_long)``.

    ``short_periods`` and ``long_periods`` are the two EMA groups of the ribbon.  The output is
    ``None`` until every EMA of both groups is seeded; then it is the minimum of the short
    group's EMAs and the maximum of the long group's EMAs, so ``lowest_short > highest_long``
    means the whole short group lies above the whole long group.
    """

    def __init__(self, short_periods: tuple[int, ...], long_periods: tuple[int, ...]) -> None:
        if not short_periods or not long_periods:
            raise ValueError("both ribbon groups need at least one period")
        self.short_periods = tuple(_period(p) for p in short_periods)
        self.long_periods = tuple(_period(p) for p in long_periods)
        self._short = [EMACalculator(p) for p in self.short_periods]
        self._long = [EMACalculator(p) for p in self.long_periods]
        self.warmup_bars = max(*self.short_periods, *self.long_periods)

    def update(self, close: Decimal) -> tuple[Decimal, Decimal] | None:
        short = [calc.update(close) for calc in self._short]
        long = [calc.update(close) for calc in self._long]
        if any(v is None for v in short) or any(v is None for v in long):
            return None
        return min(short), max(long)  # type: ignore[type-var]

    def snapshot(self) -> dict[str, object]:
        return {
            "short": [calc.snapshot() for calc in self._short],
            "long": [calc.snapshot() for calc in self._long],
        }

    @classmethod
    def restore(cls, snapshot: Mapping[str, object]) -> EMARibbonCalculator:
        short, long = snapshot["short"], snapshot["long"]
        calc = cls(
            tuple(item["period"] for item in short),  # type: ignore[union-attr,index]
            tuple(item["period"] for item in long),  # type: ignore[union-attr,index]
        )
        calc._short = [EMACalculator.restore(item) for item in short]  # type: ignore[union-attr]
        calc._long = [EMACalculator.restore(item) for item in long]  # type: ignore[union-attr]
        return calc


# ---------------------------------------------------------------------------- Keltner


class KeltnerChannelCalculator:
    """Keltner channel (Raschke modification): EMA(close) +/- multiplier * Wilder ATR.

    ``update(bar)`` returns ``(lower, middle, upper)`` once both the EMA and the ATR are seeded.
    """

    def __init__(
        self, ema_period: int = 20, atr_period: int = 10, multiplier: Decimal = Decimal(2)
    ) -> None:
        self.multiplier = _decimal(multiplier, "multiplier")
        if self.multiplier <= 0:
            raise ValueError("multiplier must be positive")
        self._ema, self._atr = EMACalculator(ema_period), SnapshotATRCalculator(atr_period)
        self.warmup_bars = max(self._ema.period, self._atr.period)

    def update(self, bar: BarLike) -> tuple[Decimal, Decimal, Decimal] | None:
        middle, atr = self._ema.update(bar.close), self._atr.update(bar)
        if middle is None or atr is None:
            return None
        with _arithmetic():
            width = self.multiplier * atr
            return middle - width, middle, middle + width

    def snapshot(self) -> dict[str, object]:
        return {
            "ema": self._ema.snapshot(), "atr": self._atr.snapshot(),
            "multiplier": str(self.multiplier),
        }

    @classmethod
    def restore(cls, snapshot: Mapping[str, object]) -> KeltnerChannelCalculator:
        ema, atr = snapshot["ema"], snapshot["atr"]
        calc = cls(ema["period"], atr["period"], _required(snapshot, "multiplier"))  # type: ignore[index]
        calc._ema = EMACalculator.restore(ema)  # type: ignore[arg-type]
        calc._atr = SnapshotATRCalculator.restore(atr)  # type: ignore[arg-type]
        return calc


# ------------------------------------------------------------------------ ConnorsRSI


class ConnorsRSICalculator:
    """Larry Connors' ConnorsRSI; ``update(close)`` returns the composite or ``None``.

    ``ConnorsRSI = (RSI(close, rsi_period) + RSI(streak, streak_period) + PercentRank) / 3``.
    ``streak`` is the signed count of consecutive up (+) or down (-) closes and 0 after an
    unchanged close.  ``PercentRank`` is the percentage of the previous ``rank_period``
    one-bar rates of change (percent) that are strictly below the current one (StockCharts
    ChartSchool, ConnorsRSI).  Both RSIs are Wilder's (:class:`RSICalculator`).
    """

    def __init__(self, rsi_period: int = 3, streak_period: int = 2, rank_period: int = 100) -> None:
        self.rank_period = _period(rank_period)
        self._rsi, self._streak_rsi = RSICalculator(rsi_period), RSICalculator(streak_period)
        self.warmup_bars = self.rank_period + 2
        self._previous: Decimal | None = None
        self._streak = 0
        self._changes: deque[Decimal] = deque(maxlen=self.rank_period)

    def update(self, close: Decimal) -> Decimal | None:
        close = _decimal(close, "close")
        price_rsi = self._rsi.update(close)
        previous, self._previous = self._previous, close
        if previous is None:
            return None
        if close > previous:
            self._streak = self._streak + 1 if self._streak > 0 else 1
        elif close < previous:
            self._streak = self._streak - 1 if self._streak < 0 else -1
        else:
            self._streak = 0
        streak_rsi = self._streak_rsi.update(Decimal(self._streak))
        with _arithmetic():
            change = _HUNDRED * (close / previous - _ONE) if previous > 0 else _ZERO
            rank: Decimal | None = None
            if len(self._changes) == self.rank_period:
                below = sum(1 for value in self._changes if value < change)
                rank = _HUNDRED * Decimal(below) / Decimal(self.rank_period)
            self._changes.append(change)
            if price_rsi is None or streak_rsi is None or rank is None:
                return None
            return (price_rsi + streak_rsi + rank) / Decimal(3)

    def snapshot(self) -> dict[str, object]:
        return {
            "rsi": self._rsi.snapshot(), "streak_rsi": self._streak_rsi.snapshot(),
            "rank_period": self.rank_period, "previous": _dump(self._previous),
            "streak": self._streak, "changes": [str(v) for v in self._changes],
        }

    @classmethod
    def restore(cls, snapshot: Mapping[str, object]) -> ConnorsRSICalculator:
        rsi, streak_rsi = snapshot["rsi"], snapshot["streak_rsi"]
        calc = cls(rsi["period"], streak_rsi["period"], snapshot["rank_period"])  # type: ignore[index,arg-type]
        calc._rsi = RSICalculator.restore(rsi)  # type: ignore[arg-type]
        calc._streak_rsi = RSICalculator.restore(streak_rsi)  # type: ignore[arg-type]
        calc._previous = _load(snapshot["previous"])
        calc._streak = int(snapshot["streak"])  # type: ignore[call-overload]
        for text in snapshot["changes"]:  # type: ignore[union-attr]
            calc._changes.append(_decimal(Decimal(text)))
        return calc


# ------------------------------------------------------------------------ Elder impulse


class ElderImpulseCalculator:
    """Alexander Elder's Impulse System; ``update(close)`` returns ``(ema, histogram, bullish)``.

    ``bullish`` (a green bar) is True when the EMA is rising *and* the MACD histogram
    (``MACD - signal``) is rising versus the previous bar, else False.  The output is ``None``
    until the histogram and the previous bar's histogram and EMA exist.
    """

    def __init__(self, ema_period: int = 13, fast: int = 12, slow: int = 26, signal: int = 9) -> None:
        self._ema = EMACalculator(ema_period)
        self._macd = MACDCalculator(fast, slow, signal)
        self.warmup_bars = self._macd.warmup_bars + 1
        self._previous: tuple[Decimal, Decimal] | None = None

    def update(self, close: Decimal) -> tuple[Decimal, Decimal, bool] | None:
        ema = self._ema.update(close)
        _, _, line, signal = self._macd.update(close)
        if ema is None or line is None or signal is None:
            return None
        with _arithmetic():
            histogram = line - signal
        previous, self._previous = self._previous, (ema, histogram)
        if previous is None:
            return None
        return ema, histogram, ema > previous[0] and histogram > previous[1]

    def snapshot(self) -> dict[str, object]:
        return {
            "ema": self._ema.snapshot(), "macd": self._macd.snapshot(),
            "previous": None if self._previous is None else [str(v) for v in self._previous],
        }

    @classmethod
    def restore(cls, snapshot: Mapping[str, object]) -> ElderImpulseCalculator:
        ema, macd = snapshot["ema"], snapshot["macd"]
        calc = cls(
            ema["period"], macd["fast"]["period"], macd["slow"]["period"],  # type: ignore[index]
            macd["signal"]["period"],  # type: ignore[index]
        )
        calc._ema = EMACalculator.restore(ema)  # type: ignore[arg-type]
        calc._macd = MACDCalculator.restore(macd)  # type: ignore[arg-type]
        previous = snapshot["previous"]
        if previous is not None:
            first, second = (_decimal(Decimal(v)) for v in previous)  # type: ignore[union-attr]
            calc._previous = (first, second)
        return calc
