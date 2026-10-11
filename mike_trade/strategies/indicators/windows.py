"""Finite-window helpers for the third BUY batch.

Like :mod:`mike_trade.strategies.indicators.finite`, every function is stateless: a result
depends only on the chronological bars supplied, nothing is padded or carried between calls,
and the last element of a window is always the latest completed bar (no later bar is read).
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, ROUND_HALF_EVEN, localcontext

from mike_trade.market_data.candles import Candle
from mike_trade.strategies.closed_bar import adjacent
from mike_trade.strategies.indicators.finite import bollinger, linear_regression_slope

__all__ = [
    "ichimoku_cloud",
    "bandwidth",
    "squeeze_on",
    "squeeze_momentum",
    "swing_pivots",
    "ease_of_movement",
    "is_hammer",
    "true_ranges",
]

_ZERO = Decimal("0")
_TWO = Decimal("2")
#: StockCharts' volume scale for the box ratio; it only rescales EMV and never moves its sign.
_EMV_VOLUME_SCALE = Decimal(100_000_000)


def _midpoint(bars: Sequence[Candle]) -> Decimal:
    return (max(bar.high for bar in bars) + min(bar.low for bar in bars)) / _TWO


def ichimoku_cloud(
    bars: Sequence[Candle], tenkan: int, kijun: int, senkou_b: int, displacement: int
) -> tuple[Decimal, Decimal]:
    """Senkou Span A and B *as plotted at the latest bar* (``bars[-1]``).

    Hosoda spans are computed from data at bar ``j`` and plotted ``displacement`` bars ahead
    (StockCharts ChartSchool, Ichimoku Cloud), so the cloud at the latest bar was computed
    from the window ending ``displacement`` bars earlier: no bar after that point is used.
    ``Span A = (Tenkan + Kijun) / 2``, ``Span B`` = midpoint of the ``senkou_b``-bar range,
    Tenkan/Kijun being the midpoints of their own ranges.  Needs ``senkou_b + displacement``
    bars.
    """
    if len(bars) < max(senkou_b, kijun, tenkan) + displacement:
        raise ValueError("not enough bars for the displaced Ichimoku cloud")
    source = bars[: len(bars) - displacement]
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = 28, ROUND_HALF_EVEN
        span_a = (_midpoint(source[-tenkan:]) + _midpoint(source[-kijun:])) / _TWO
        span_b = _midpoint(source[-senkou_b:])
        return span_a, span_b


def bandwidth(closes: Sequence[Decimal], period: int, deviations: Decimal) -> Decimal | None:
    """Bollinger BandWidth ``(upper - lower) / middle``; ``None`` if the middle band is not > 0."""
    lower, middle, upper = bollinger(closes, period, deviations)
    if middle <= 0:
        return None
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = 28, ROUND_HALF_EVEN
        return (upper - lower) / middle


def true_ranges(bars: Sequence[Candle]) -> list[Decimal]:
    """True range of every bar after the first (the first only supplies the prior close)."""
    return [
        max(bar.high - bar.low, abs(bar.high - prev.close), abs(bar.low - prev.close))
        for prev, bar in zip(bars, bars[1:])
    ]


def squeeze_on(
    bars: Sequence[Candle], period: int, bb_deviations: Decimal, kc_multiplier: Decimal
) -> bool:
    """John Carter's squeeze: Bollinger Bands lie entirely inside the Keltner Channel.

    ``bars`` is ``period + 1`` bars ending at the bar of interest.  Bollinger: SMA(close) +/-
    ``bb_deviations`` population standard deviations.  Keltner: SMA(close) +/- ``kc_multiplier``
    x SMA(true range), all over the last ``period`` bars.
    """
    if len(bars) != period + 1:
        raise ValueError("squeeze window must be period + 1 bars")
    window = bars[1:]
    bb_lower, middle, bb_upper = bollinger([bar.close for bar in window], period, bb_deviations)
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = 28, ROUND_HALF_EVEN
        width = kc_multiplier * sum(true_ranges(bars), _ZERO) / Decimal(period)
        return bb_lower > middle - width and bb_upper < middle + width


def squeeze_momentum(bars: Sequence[Candle], period: int) -> Decimal:
    """Carter's squeeze momentum: linear-regression value of ``close - mean(mid-range, SMA)``.

    ``bars`` is ``2 * period - 1`` bars ending at the bar of interest.  For each of the last
    ``period`` bars the source is ``close - ((highest high + lowest low) / 2 + SMA(close)) / 2``
    over the ``period`` bars ending there; the result is the least-squares line's value at the
    latest bar.
    """
    if len(bars) != 2 * period - 1:
        raise ValueError("momentum window must be 2 * period - 1 bars")
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = 28, ROUND_HALF_EVEN
        source: list[Decimal] = []
        for end in range(period, len(bars) + 1):
            window = bars[end - period : end]
            mean = sum((bar.close for bar in window), _ZERO) / Decimal(period)
            source.append(window[-1].close - (_midpoint(window) + mean) / _TWO)
        count = Decimal(period)
        slope = linear_regression_slope(source)
        average = sum(source, _ZERO) / count
        return average + slope * ((count - 1) / _TWO)


def swing_pivots(
    bars: Sequence[Candle], strength: int
) -> tuple[list[tuple[int, Decimal]], list[tuple[int, Decimal]]]:
    """Confirmed fractal swing ``(highs, lows)`` as chronological ``(index, price)`` lists.

    Bill Williams fractal generalized to ``strength`` bars per side: a high (low) is a swing
    when it is strictly above (below) the highs (lows) of the ``strength`` bars on each side.
    A pivot is reported only when all ``2 * strength + 1`` bars lie inside ``bars`` and are
    consecutive completed bars, so it is confirmed by bars already closed and never uses a
    later one.
    """
    highs: list[tuple[int, Decimal]] = []
    lows: list[tuple[int, Decimal]] = []
    for center in range(strength, len(bars) - strength):
        group = bars[center - strength : center + strength + 1]
        if any(not adjacent(a, b) for a, b in zip(group, group[1:])):
            continue
        others = [bar for i, bar in enumerate(group) if i != strength]
        if bars[center].high > max(bar.high for bar in others):
            highs.append((center, bars[center].high))
        if bars[center].low < min(bar.low for bar in others):
            lows.append((center, bars[center].low))
    return highs, lows


def ease_of_movement(bars: Sequence[Candle], period: int) -> Decimal | None:
    """Richard Arms' Ease of Movement, simple-averaged over ``period`` one-bar values.

    ``EMV = ((H+L)/2 - (H'+L')/2) / ((V / scale) / (H - L))``.  A zero-range bar has an
    infinite box ratio, so its EMV is the limit 0; a zero-volume bar has no defined EMV and
    makes the result ``None``.  ``bars`` is ``period + 1`` bars.
    """
    if len(bars) != period + 1:
        raise ValueError("EMV window must be period + 1 bars")
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = 28, ROUND_HALF_EVEN
        total = _ZERO
        for prev, bar in zip(bars, bars[1:]):
            if bar.volume <= 0:
                return None
            distance = (bar.high + bar.low) / _TWO - (prev.high + prev.low) / _TWO
            total += distance * (bar.high - bar.low) * _EMV_VOLUME_SCALE / bar.volume
        return total / Decimal(period)


def is_hammer(bar: Candle, shadow_ratio: Decimal, max_upper_fraction: Decimal) -> bool:
    """Nison hammer shape: long lower shadow, small body at the top, negligible upper shadow.

    ``lower shadow >= shadow_ratio * real body`` and ``upper shadow <= max_upper_fraction *
    (high - low)`` on a bar with a non-zero range.
    """
    spread = bar.high - bar.low
    if spread <= 0:
        return False
    body = abs(bar.close - bar.open)
    lower = min(bar.open, bar.close) - bar.low
    upper = bar.high - max(bar.open, bar.close)
    return lower >= shadow_ratio * body and upper <= max_upper_fraction * spread and lower > 0
