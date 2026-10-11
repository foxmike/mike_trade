"""Finite-window helpers for BUY batch 4: Ultimate Oscillator, z-score and trendline geometry.

Stateless like :mod:`mike_trade.strategies.indicators.windows`: results depend only on the
chronological bars supplied and nothing after the last element of a window is read.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal, ROUND_HALF_EVEN, localcontext

from mike_trade.market_data.candles import Candle
from mike_trade.strategies.closed_bar import adjacent

__all__ = ["ultimate_oscillator", "z_score", "line_value", "contiguous", "first_close_above"]

_ZERO = Decimal("0")


def ultimate_oscillator(
    bars: Sequence[Candle], short: int = 7, medium: int = 14, long: int = 28
) -> list[Decimal | None]:
    """Larry Williams' Ultimate Oscillator aligned with ``bars`` (``None`` where undefined).

    ``BP = close - min(low, prior close)``, ``TR = max(high, prior close) - min(low, prior close)``,
    ``Average_n = sum(BP, n) / sum(TR, n)`` and ``UO = 100 * (4*A_short + 2*A_medium + A_long) / 7``.
    The first value is on index ``long`` (it needs a prior close); a window whose true-range sum is 0
    makes that bar's value ``None``.
    """
    out: list[Decimal | None] = [None] * len(bars)
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = 28, ROUND_HALF_EVEN
        pressure = [_ZERO]
        ranges = [_ZERO]
        for prev, bar in zip(bars, bars[1:]):
            low, high = min(bar.low, prev.close), max(bar.high, prev.close)
            pressure.append(bar.close - low)
            ranges.append(high - low)
        for i in range(long, len(bars)):
            averages = []
            for n in (short, medium, long):
                total = sum(ranges[i - n + 1:i + 1], _ZERO)
                if total == 0:
                    break
                averages.append(sum(pressure[i - n + 1:i + 1], _ZERO) / total)
            else:
                out[i] = 100 * (4 * averages[0] + 2 * averages[1] + averages[2]) / 7
    return out


def z_score(closes: Sequence[Decimal]) -> Decimal | None:
    """``(last - mean) / population standard deviation`` of the window; ``None`` if flat."""
    if len(closes) < 2:
        raise ValueError("z-score needs at least two closes")
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = 28, ROUND_HALF_EVEN
        count = Decimal(len(closes))
        mean = sum(closes, _ZERO) / count
        variance = sum(((v - mean) ** 2 for v in closes), _ZERO) / count
        if variance == 0:
            return None
        return (closes[-1] - mean) / variance.sqrt()


def line_value(x1: int, y1: Decimal, x2: int, y2: Decimal, x: int) -> Decimal:
    """Value at ``x`` of the straight line through ``(x1, y1)`` and ``(x2, y2)``."""
    if x1 == x2:
        raise ValueError("a line needs two distinct x positions")
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = 28, ROUND_HALF_EVEN
        return y1 + (y2 - y1) * Decimal(x - x1) / Decimal(x2 - x1)


def contiguous(bars: Sequence[Candle]) -> bool:
    """True when every bar starts exactly where the previous one ended (no missing minute)."""
    return all(adjacent(a, b) for a, b in zip(bars, bars[1:]))


def first_close_above(
    bars: Sequence[Candle], line: Callable[[int], Decimal], since: int
) -> bool:
    """The last bar closes above ``line`` and no bar from ``since`` up to the one before it did.

    ``line`` maps a bar index of ``bars`` to the level at that bar, so a horizontal level and a
    sloped trendline are handled alike.  Bars are compared on their closes only.
    """
    last = len(bars) - 1
    return (0 <= since <= last and bars[last].close > line(last)
            and all(bars[x].close <= line(x) for x in range(since, last)))
