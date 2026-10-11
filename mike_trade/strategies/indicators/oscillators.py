"""Resumable oscillators for BUY batch 4: TSI, Stochastic RSI and an ADX/EMA pair.

Same contract as :mod:`mike_trade.strategies.indicators.extended`: Decimal arithmetic in the
shared context, ``None`` until the seed is complete, JSON-safe ``snapshot()``/``restore()``.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from decimal import Decimal

from mike_trade.strategies.indicators.core import (
    EMACalculator,
    RSICalculator,
    _HLC,
    _arithmetic,
    _decimal,
    _dump,
    _load,
    _period,
)
from mike_trade.strategies.indicators.extended import ADXCalculator

__all__ = ["TSICalculator", "StochRSICalculator", "ADXEMACalculator"]

_ZERO = Decimal("0")
_HUNDRED = Decimal("100")


class TSICalculator:
    """William Blau's True Strength Index; ``update(close)`` returns TSI (percent) or ``None``.

    ``TSI = 100 * EMA(EMA(change, long), short) / EMA(EMA(|change|, long), short)`` with
    ``change = close - previous close``.  Every EMA is the shared SMA-seeded EMA, so the first
    TSI appears on bar ``long + short`` (count) and is 0 when the absolute-change average is 0.
    """

    def __init__(self, long_period: int = 25, short_period: int = 13) -> None:
        self.long_period, self.short_period = _period(long_period), _period(short_period)
        self.warmup_bars = self.long_period + self.short_period
        self._previous: Decimal | None = None
        self._smoothers = [EMACalculator(p) for p in (self.long_period, self.short_period) * 2]

    def update(self, close: Decimal) -> Decimal | None:
        close = _decimal(close, "close")
        previous, self._previous = self._previous, close
        if previous is None:
            return None
        with _arithmetic():
            change = close - previous
            magnitude = abs(change)
        first = self._smoothers[0].update(change)
        second = self._smoothers[2].update(magnitude)
        double = double_abs = None
        if first is not None:
            double = self._smoothers[1].update(first)
        if second is not None:
            double_abs = self._smoothers[3].update(second)
        if double is None or double_abs is None:
            return None
        with _arithmetic():
            return _ZERO if double_abs == 0 else _HUNDRED * double / double_abs

    def snapshot(self) -> dict[str, object]:
        return {
            "long": self.long_period, "short": self.short_period,
            "previous": _dump(self._previous),
            "smoothers": [s.snapshot() for s in self._smoothers],
        }

    @classmethod
    def restore(cls, snapshot: Mapping[str, object]) -> TSICalculator:
        calc = cls(snapshot["long"], snapshot["short"])  # type: ignore[arg-type]
        calc._previous = _load(snapshot["previous"])
        calc._smoothers = [EMACalculator.restore(s) for s in snapshot["smoothers"]]  # type: ignore[union-attr]
        return calc


class StochRSICalculator:
    """Chande & Kroll Stochastic RSI; ``update(close)`` returns a value in ``[0, 1]`` or ``None``.

    ``StochRSI = (RSI - lowest RSI) / (highest RSI - lowest RSI)`` over the latest
    ``stoch_period`` Wilder RSI values (current included).  A flat RSI window is undefined
    (``None``).
    """

    def __init__(self, rsi_period: int = 14, stoch_period: int = 14) -> None:
        self._rsi = RSICalculator(rsi_period)
        self.rsi_period, self.stoch_period = self._rsi.period, _period(stoch_period)
        self.warmup_bars = self.rsi_period + self.stoch_period
        self._values: deque[Decimal] = deque(maxlen=self.stoch_period)

    def update(self, close: Decimal) -> Decimal | None:
        rsi = self._rsi.update(close)
        if rsi is None:
            return None
        self._values.append(rsi)
        if len(self._values) < self.stoch_period:
            return None
        low, high = min(self._values), max(self._values)
        if high == low:
            return None
        with _arithmetic():
            return (rsi - low) / (high - low)

    def snapshot(self) -> dict[str, object]:
        return {
            "rsi": self._rsi.snapshot(), "stoch": self.stoch_period,
            "values": [str(v) for v in self._values],
        }

    @classmethod
    def restore(cls, snapshot: Mapping[str, object]) -> StochRSICalculator:
        rsi = snapshot["rsi"]
        calc = cls(rsi["period"], snapshot["stoch"])  # type: ignore[index,arg-type]
        calc._rsi = RSICalculator.restore(rsi)  # type: ignore[arg-type]
        for text in snapshot["values"]:  # type: ignore[union-attr]
            calc._values.append(_decimal(Decimal(text)))
        return calc


class ADXEMACalculator:
    """ADX plus an EMA of the close; ``update(bar)`` returns ``(+DI, -DI, ADX, EMA, adx_rising)``.

    ``adx_rising`` is True when this bar's ADX exceeds the previous bar's.  The output is
    ``None`` until the EMA, ADX and a previous ADX all exist, and while the smoothed true range is
    zero (no DI).
    """

    def __init__(self, adx_period: int = 14, ema_period: int = 20) -> None:
        self._adx, self._ema = ADXCalculator(adx_period), EMACalculator(ema_period)
        self.warmup_bars = max(self._adx.warmup_bars + 1, self._ema.period)
        self._previous_adx: Decimal | None = None

    def update(self, bar: _HLC) -> tuple[Decimal, Decimal, Decimal, Decimal, bool] | None:
        plus, minus, adx = self._adx.update(bar)
        ema = self._ema.update(bar.close)
        previous, self._previous_adx = self._previous_adx, adx
        if None in (plus, minus, adx, ema, previous):
            return None
        return plus, minus, adx, ema, adx > previous  # type: ignore[operator,return-value]

    def snapshot(self) -> dict[str, object]:
        return {
            "adx": self._adx.snapshot(), "ema": self._ema.snapshot(),
            "previous_adx": _dump(self._previous_adx),
        }

    @classmethod
    def restore(cls, snapshot: Mapping[str, object]) -> ADXEMACalculator:
        adx, ema = snapshot["adx"], snapshot["ema"]
        calc = cls(adx["dmi"]["period"], ema["period"])  # type: ignore[index]
        calc._adx = ADXCalculator.restore(adx)  # type: ignore[arg-type]
        calc._ema = EMACalculator.restore(ema)  # type: ignore[arg-type]
        calc._previous_adx = _load(snapshot["previous_adx"])
        return calc
