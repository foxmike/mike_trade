"""Finite-window indicator helpers used by individual technical strategies.

These routines deliberately avoid hidden recursive state.  Callers supply the
chronological observations they want evaluated and each result depends only on
that finite window.  They do not pad missing bars or maintain cross-call state.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from math import isqrt

from mike_trade.market_data.candles import Candle

_ZERO = Decimal("0")
_ONE = Decimal("1")
_HUNDRED = Decimal("100")


def bollinger(closes: Sequence[Decimal], period: int, deviations: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """Return population-SD Bollinger ``(lower, middle, upper)`` for one window."""
    if len(closes) != period or period < 2 or deviations <= 0:
        raise ValueError("Bollinger window must match a period >= 2 and deviations > 0")
    with localcontext() as ctx:
        ctx.prec = 28
        ctx.rounding = ROUND_HALF_EVEN
        count = Decimal(period)
        mean = sum(closes, _ZERO) / count
        variance = sum(((v - mean) ** 2 for v in closes), _ZERO) / count
        # Decimal has no square-root context dependence when called within this context.
        width = variance.sqrt() * deviations
        return mean - width, mean, mean + width


def money_flow_index(bars: Sequence[Candle], period: int) -> Decimal | None:
    """Wilder/StockCharts MFI for ``period`` typical-price comparisons.

    Ties contribute to neither positive nor negative flow.  With no signed flow
    the index is undefined; one-sided flow maps to its mathematical 0/100 limit.
    """
    if period < 1 or len(bars) != period + 1:
        raise ValueError("MFI needs period + 1 bars")
    with localcontext() as ctx:
        ctx.prec = 28
        ctx.rounding = ROUND_HALF_EVEN
        positive = negative = _ZERO
        previous = (bars[0].high + bars[0].low + bars[0].close) / Decimal(3)
        for bar in bars[1:]:
            typical = (bar.high + bar.low + bar.close) / Decimal(3)
            flow = typical * bar.volume
            if typical > previous:
                positive += flow
            elif typical < previous:
                negative += flow
            previous = typical
        if positive == 0 and negative == 0:
            return None
        if negative == 0:
            return _HUNDRED
        if positive == 0:
            return _ZERO
        ratio = positive / negative
        return _HUNDRED - (_HUNDRED / (_ONE + ratio))


def chaikin_money_flow(bars: Sequence[Candle]) -> Decimal | None:
    """Return rolling CMF; zero-range bars contribute zero money-flow volume."""
    if not bars:
        raise ValueError("CMF needs at least one bar")
    with localcontext() as ctx:
        ctx.prec = 28
        ctx.rounding = ROUND_HALF_EVEN
        volume_sum = sum((bar.volume for bar in bars), _ZERO)
        if volume_sum == 0:
            return None
        flow_volume = _ZERO
        for bar in bars:
            spread = bar.high - bar.low
            multiplier = _ZERO if spread == 0 else (
                (bar.close - bar.low) - (bar.high - bar.close)
            ) / spread
            flow_volume += multiplier * bar.volume
        return flow_volume / volume_sum


def engulfing(first: Candle, second: Candle, *, bullish: bool) -> bool:
    """Strict real-body engulfing; shadows are intentionally excluded."""
    if bullish:
        return (
            first.close < first.open
            and second.close > second.open
            and second.open <= first.close
            and second.close >= first.open
        )
    return (
        first.close > first.open
        and second.close < second.open
        and second.open >= first.close
        and second.close <= first.open
    )


def weighted_moving_average(values: Sequence[Decimal]) -> Decimal:
    """Recent observations receive weights 1 through n (oldest to newest)."""
    if not values:
        raise ValueError("WMA needs at least one value")
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = 28, ROUND_HALF_EVEN
        count = len(values)
        return sum((value * index for index, value in enumerate(values, 1)), _ZERO) / Decimal(
            count * (count + 1) // 2
        )


def rate_of_change(current: Decimal, earlier: Decimal) -> Decimal:
    """Percentage change from a strictly positive earlier close."""
    if earlier <= 0:
        raise ValueError("earlier close must be positive")
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = 28, ROUND_HALF_EVEN
        return _HUNDRED * (current / earlier - _ONE)


def hull_smoothing_period(period: int) -> int:
    """Nearest integer to sqrt(period), with halves rounded upward."""
    root = isqrt(period)
    return root + (period >= root * root + root + 1)


def hull_moving_average(closes: Sequence[Decimal], period: int) -> Decimal:
    """Alan Hull's WMA(sqrt(n), 2 WMA(n/2) - WMA(n)) for an exact window."""
    if period < 4:
        raise ValueError("HMA period must be at least 4")
    half = (period + 1) // 2
    smoothing = hull_smoothing_period(period)
    if len(closes) != period + smoothing - 1:
        raise ValueError("HMA window length must be period + rounded sqrt(period) - 1")
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = 28, ROUND_HALF_EVEN
        raw: list[Decimal] = []
        for end in range(period, len(closes) + 1):
            window = closes[end - period : end]
            raw.append(2 * weighted_moving_average(window[-half:])
                       - weighted_moving_average(window))
        return weighted_moving_average(raw)


def linear_regression_slope(values: Sequence[Decimal]) -> Decimal:
    """Least-squares price change per bar, with x=0..n-1 in chronological order."""
    count = len(values)
    if count < 2:
        raise ValueError("regression slope needs at least two values")
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = 28, ROUND_HALF_EVEN
        sx = Decimal(count * (count - 1) // 2)
        sxx = Decimal(count * (count - 1) * (2 * count - 1) // 6)
        sy = sum(values, _ZERO)
        sxy = sum((Decimal(i) * value for i, value in enumerate(values)), _ZERO)
        return (Decimal(count) * sxy - sx * sy) / (Decimal(count) * sxx - sx * sx)


def awesome_oscillator(bars: Sequence[Candle]) -> Decimal:
    """Williams AO: SMA(5, median price) minus SMA(34, median price)."""
    if len(bars) != 34:
        raise ValueError("Awesome Oscillator needs exactly 34 bars")
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = 28, ROUND_HALF_EVEN
        median = [(bar.high + bar.low) / 2 for bar in bars]
        return sum(median[-5:], _ZERO) / Decimal(5) - sum(median, _ZERO) / Decimal(34)


def original_keltner(bars: Sequence[Candle]) -> tuple[Decimal, Decimal, Decimal]:
    """Chester Keltner's original SMA(typical price) +/- SMA(high-low range)."""
    if len(bars) < 2:
        raise ValueError("Keltner channel needs at least two bars")
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = 28, ROUND_HALF_EVEN
        count = Decimal(len(bars))
        middle = sum(((bar.high + bar.low + bar.close) / 3 for bar in bars), _ZERO) / count
        width = sum((bar.high - bar.low for bar in bars), _ZERO) / count
        return middle - width, middle, middle + width
