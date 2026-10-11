"""Cumulative volume lines (OBV, Accumulation/Distribution, Volume Price Trend) with persistence.

Each line is an unbounded running sum, so its absolute level depends on the first bar fed.  The
strategies built on it only compare the line with its own recent history, which is invariant to
that starting level; the running sum itself is resumed from a snapshot by
:func:`mike_trade.strategies.closed_bar.advance`, never recomputed from a moving candle window.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from decimal import Decimal

from mike_trade.strategies.indicators.core import BarLike, _arithmetic, _decimal, _dump, _load, _period

__all__ = ["CUMULATIVE_KINDS", "CumulativeLineCalculator"]

CUMULATIVE_KINDS = ("obv", "ad", "vpt")
_ZERO = Decimal("0")


class CumulativeLineCalculator:
    """Running OBV, A/D line or VPT; ``update(bar)`` returns ``(line, highest_of_previous_n)``.

    * ``obv`` (Granville): add the bar's volume on an up close, subtract it on a down close,
      nothing on an unchanged close; the first bar adds nothing.
    * ``ad`` (Chaikin / StockCharts): add ``((C-L)-(H-C))/(H-L) * volume``; a zero-range bar adds 0.
    * ``vpt``: add ``volume * (C - C_prev) / C_prev``; the first bar adds nothing.

    The output is ``None`` until ``period`` earlier line values exist; the second element is the
    highest of those ``period`` earlier values (the current value is excluded).
    """

    def __init__(self, kind: str, period: int) -> None:
        if kind not in CUMULATIVE_KINDS:
            raise ValueError(f"kind must be one of {CUMULATIVE_KINDS}")
        self.kind, self.period = kind, _period(period)
        self.warmup_bars = self.period + 1
        self._line = _ZERO
        self._close: Decimal | None = None
        self._recent: deque[Decimal] = deque(maxlen=self.period)

    def update(self, bar: BarLike) -> tuple[Decimal, Decimal] | None:
        close = _decimal(bar.close, "close")
        volume = _decimal(bar.volume, "volume")
        with _arithmetic():
            if self.kind == "ad":
                spread = bar.high - bar.low
                if spread != 0:
                    self._line += ((close - bar.low) - (bar.high - close)) / spread * volume
            elif self._close is not None:
                if self.kind == "obv":
                    self._line += volume if close > self._close else -volume if close < self._close else _ZERO
                elif self._close > 0:
                    self._line += volume * (close - self._close) / self._close
        self._close = close
        out = (self._line, max(self._recent)) if len(self._recent) == self.period else None
        self._recent.append(self._line)
        return out

    def snapshot(self) -> dict[str, object]:
        return {
            "kind": self.kind, "period": self.period, "line": str(self._line),
            "close": _dump(self._close), "recent": [str(v) for v in self._recent],
        }

    @classmethod
    def restore(cls, snapshot: Mapping[str, object]) -> CumulativeLineCalculator:
        calc = cls(snapshot["kind"], snapshot["period"])  # type: ignore[arg-type]
        line = _load(snapshot["line"])
        if line is None:
            raise ValueError("inconsistent cumulative-line snapshot")
        calc._line, calc._close = line, _load(snapshot["close"])
        for text in snapshot["recent"]:  # type: ignore[union-attr]
            calc._recent.append(_decimal(Decimal(text)))
        return calc
