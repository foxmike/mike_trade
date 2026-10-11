"""Deterministic market metrics calculator.

Computes broad-market drawdown, realized volatility, price momentum, breadth
deterioration, and stabilization/recovery indicators across reference symbols
using isolated Decimal arithmetic.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from types import MappingProxyType
from typing import Iterator

from mike_trade.market_data.candles import Candle
from mike_trade.market_status.config import MarketStatusConfig

__all__ = [
    "MarketMetricsSnapshot",
    "calculate_market_metrics",
]

_PRECISION = 28
_ZERO = Decimal("0")
_ONE = Decimal("1")
_HUNDRED = Decimal("100")
_BPS_SCALE = Decimal("10000")


@contextmanager
def _arithmetic() -> Iterator[None]:
    """Isolated Decimal arithmetic context."""
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        ctx.rounding = ROUND_HALF_EVEN
        yield


@dataclass(frozen=True, slots=True)
class MarketMetricsSnapshot:
    """Point-in-time calculation of broad market risk metrics."""

    as_of: datetime
    composite_drawdown_percent: Decimal
    worst_drawdown_percent: Decimal
    realized_volatility_bps: Decimal
    momentum_bps: Decimal
    breadth_weakness_ratio: Decimal
    drawdown_by_symbol: Mapping[str, Decimal] = field(default_factory=dict)
    volatility_by_symbol: Mapping[str, Decimal] = field(default_factory=dict)
    momentum_by_symbol: Mapping[str, Decimal] = field(default_factory=dict)
    trough_drawdown_percent: Decimal = _ZERO
    bars_since_trough: int = 0
    peak_volatility_bps: Decimal = _ZERO
    volatility_decay_ratio: Decimal = _ONE
    rebound_from_trough_percent: Decimal = _ZERO
    vix_level: Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.as_of, datetime) or self.as_of.tzinfo is None:
            raise ValueError("as_of must be a timezone-aware datetime")
        object.__setattr__(self, "as_of", self.as_of.astimezone(UTC))
        object.__setattr__(
            self, "drawdown_by_symbol", MappingProxyType(dict(self.drawdown_by_symbol))
        )
        object.__setattr__(
            self, "volatility_by_symbol", MappingProxyType(dict(self.volatility_by_symbol))
        )
        object.__setattr__(
            self, "momentum_by_symbol", MappingProxyType(dict(self.momentum_by_symbol))
        )


def _symbol_drawdown(bars: Sequence[Candle], lookback: int) -> tuple[Decimal, Decimal]:
    """Return (drawdown_percent, peak_high) over the given lookback window."""
    with _arithmetic():
        series = list(bars)
        window = series[-lookback:] if len(series) > lookback else series
        peak_high = max(bar.high for bar in window)
        latest_close = series[-1].close
        if peak_high <= _ZERO:
            return _ZERO, _ZERO
        drawdown = ((latest_close - peak_high) / peak_high) * _HUNDRED
        return drawdown, peak_high


def _symbol_realized_volatility_bps(bars: Sequence[Candle], window_size: int) -> Decimal:
    """Calculate sample standard deviation of close returns in basis points."""
    with _arithmetic():
        series = list(bars)
        if len(series) < window_size or window_size < 2:
            return _ZERO
        window = series[-window_size:]
        returns: list[Decimal] = []
        for i in range(1, len(window)):
            prev_close = window[i - 1].close
            curr_close = window[i].close
            if prev_close > _ZERO:
                returns.append((curr_close - prev_close) / prev_close)

        n = len(returns)
        if n < 2:
            return _ZERO
        mean_r = sum(returns, _ZERO) / Decimal(n)
        variance = sum((r - mean_r) ** 2 for r in returns) / Decimal(n - 1)
        if variance <= _ZERO:
            return _ZERO
        stdev = variance.sqrt()
        return stdev * _BPS_SCALE


def _symbol_momentum_bps(bars: Sequence[Candle], window_size: int) -> Decimal:
    """Calculate percentage difference between close and SMA in basis points."""
    with _arithmetic():
        series = list(bars)
        if len(series) < window_size or window_size < 1:
            return _ZERO
        window = series[-window_size:]
        sma = sum((b.close for b in window), _ZERO) / Decimal(len(window))
        if sma <= _ZERO:
            return _ZERO
        latest_close = series[-1].close
        return ((latest_close - sma) / sma) * _BPS_SCALE


def calculate_market_metrics(
    symbol_histories: Mapping[str, Sequence[Candle]],
    as_of: datetime,
    config: MarketStatusConfig,
    *,
    prior_trough_drawdown: Decimal | None = None,
    prior_bars_since_trough: int = 0,
    prior_peak_volatility_bps: Decimal | None = None,
    vix_level: Decimal | None = None,
) -> MarketMetricsSnapshot:
    """Compute market risk metrics across available reference symbols."""
    with _arithmetic():
        active_symbols = [
            s for s in config.symbols.symbols
            if s in symbol_histories and len(symbol_histories[s]) > 0
        ]

        if not active_symbols:
            return MarketMetricsSnapshot(
                as_of=as_of,
                composite_drawdown_percent=_ZERO,
                worst_drawdown_percent=_ZERO,
                realized_volatility_bps=_ZERO,
                momentum_bps=_ZERO,
                breadth_weakness_ratio=_ZERO,
                drawdown_by_symbol={},
                volatility_by_symbol={},
                momentum_by_symbol={},
                vix_level=vix_level,
            )

        drawdowns: dict[str, Decimal] = {}
        volatilities: dict[str, Decimal] = {}
        momentums: dict[str, Decimal] = {}
        weak_count = 0

        for sym in active_symbols:
            bars = symbol_histories[sym]
            dd, _ = _symbol_drawdown(bars, config.windows.peak_lookback_bars)
            vol = _symbol_realized_volatility_bps(bars, config.windows.volatility_window_bars)
            mom = _symbol_momentum_bps(bars, config.windows.momentum_window_bars)

            drawdowns[sym] = dd
            volatilities[sym] = vol
            momentums[sym] = mom

            # A symbol is marked weak if in drawdown below warning threshold or momentum negative
            if dd <= config.drawdowns.warning_percent or mom < _ZERO:
                weak_count += 1

        count_dec = Decimal(len(active_symbols))
        composite_dd = sum(drawdowns.values(), _ZERO) / count_dec
        worst_dd = min(drawdowns.values())
        composite_vol = sum(volatilities.values(), _ZERO) / count_dec
        composite_mom = sum(momentums.values(), _ZERO) / count_dec
        breadth_ratio = Decimal(weak_count) / count_dec

        # Episode tracking for stabilization and recovery:
        # Trough drawdown is the minimum (most negative) composite drawdown
        if prior_trough_drawdown is None or composite_dd < prior_trough_drawdown:
            trough_dd = composite_dd
            bars_since_trough = 0
        else:
            trough_dd = prior_trough_drawdown
            bars_since_trough = prior_bars_since_trough + 1

        # Peak volatility tracking during episode
        if prior_peak_volatility_bps is None or composite_vol > prior_peak_volatility_bps:
            peak_vol = composite_vol
        else:
            peak_vol = prior_peak_volatility_bps

        vol_decay = composite_vol / peak_vol if peak_vol > _ZERO else _ONE
        rebound_percent = composite_dd - trough_dd

        return MarketMetricsSnapshot(
            as_of=as_of,
            composite_drawdown_percent=composite_dd,
            worst_drawdown_percent=worst_dd,
            realized_volatility_bps=composite_vol,
            momentum_bps=composite_mom,
            breadth_weakness_ratio=breadth_ratio,
            drawdown_by_symbol=drawdowns,
            volatility_by_symbol=volatilities,
            momentum_by_symbol=momentums,
            trough_drawdown_percent=trough_dd,
            bars_since_trough=bars_since_trough,
            peak_volatility_bps=peak_vol,
            volatility_decay_ratio=vol_decay,
            rebound_from_trough_percent=rebound_percent,
            vix_level=vix_level,
        )
