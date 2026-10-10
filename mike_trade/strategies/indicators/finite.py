"""Finite-window indicator helpers used by individual technical strategies.

These routines deliberately avoid hidden recursive state.  Callers supply the
chronological observations they want evaluated and each result depends only on
that finite window.  They do not pad missing bars or maintain cross-call state.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, ROUND_HALF_EVEN, localcontext

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
