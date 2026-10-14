"""Versioned per-candle window features over aggregated (N-minute) candles.

A separate feature family (``intraday_candle_window``) from ``intraday_core``: the original
one-minute model's definitions are untouched.  Every feature is a function of the most recent
``lookback_bars`` completed candles of one session and nothing older, so the history a model needs
is exactly the configured lookback and a window never crosses a session boundary or a missing
candle.  The pipeline has the same ``update``/``transform`` shape as ``IntradayFeaturePipeline``:
batch training is the streaming implementation applied to a sequence (there is no second
implementation to drift).

Window position is part of every feature name: ``lag0`` is the most recent completed candle,
``lag1`` the one before it, ... ``lag{N-1}`` the oldest.  Per candle ``k``:

* ``lag{k}_body_pct``       (close - open) / open
* ``lag{k}_upper_wick``     (high - max(open, close)) / (high - low)      [0 if high == low]
* ``lag{k}_lower_wick``     (min(open, close) - low) / (high - low)       [0 if high == low]
* ``lag{k}_range_pct``      (high - low) / close
* ``lag{k}_relative_volume`` volume / mean volume of the window's candles
* ``lag{k}_return``         close / previous candle's close - 1           [k = 0 .. N-2]
* ``lag{k}_acceleration``   lag{k}_return - lag{k+1}_return               [k = 0 .. N-3]

(The oldest candle has no previous close inside the window, and the two oldest no earlier return, so
those entries do not exist rather than borrowing history beyond the lookback.)

Recency-weighted summaries use exponential weights ``w_k = 0.5 ** (k / half_life_bars)`` with ``k``
the lag (the latest candle weighs 1, one half-life older weighs 0.5, ...), normalized over the lags
a metric exists for: ``rw_<metric>`` for body, wicks, range, relative volume, return, acceleration.
The half-life is configuration.  These weight *candles within the input window*; they are not
sample weights over training dates.

Window features: ``window_return`` (latest close / oldest open - 1), ``window_range_pct``,
``window_close_position`` (where the latest close sits in the window's range), ``window_volatility``
(population standard deviation of close-to-close log returns), ``close_vs_window_mean``;
``vwap_distance`` from the latest candle's volume-weighted VWAP; ``time_of_day_sin/cos`` of the
latest candle's end; and ``relative_strength_<ref>`` against reference symbols aggregated with the
same grid.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from statistics import fmean, pstdev
from zoneinfo import ZoneInfo

from mike_trade.modeling.contracts import FeatureSchema, FeatureVector, ModelBar

__all__ = [
    "CANDLE_WINDOW_FEATURE_ID",
    "CandleWindowFeatureConfig",
    "CandleWindowFeaturePipeline",
]

CANDLE_WINDOW_FEATURE_ID = "intraday_candle_window"
_NY = ZoneInfo("America/New_York")
_PER_CANDLE = ("body_pct", "upper_wick", "lower_wick", "range_pct", "relative_volume")
_SUMMARIZED = (*_PER_CANDLE, "return", "acceleration")


@dataclass(frozen=True, slots=True)
class CandleWindowFeatureConfig:
    """Versioned definition of the candle-window feature family."""

    bar_minutes: int
    lookback_bars: int
    half_life_bars: float
    feature_id: str = CANDLE_WINDOW_FEATURE_ID
    version: str = "1.0.0"
    reference_symbols: tuple[str, ...] = ()
    include_vwap: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.bar_minutes, bool) or not isinstance(self.bar_minutes, int):
            raise ValueError("bar_minutes must be an int")
        if self.bar_minutes < 2:
            raise ValueError("bar_minutes must be >= 2 (one-minute models use intraday_core)")
        if (
            isinstance(self.lookback_bars, bool)
            or not isinstance(self.lookback_bars, int)
            or self.lookback_bars < 4
        ):
            raise ValueError("lookback_bars must be an int >= 4")
        if (
            isinstance(self.half_life_bars, bool)
            or not isinstance(self.half_life_bars, (int, float))
            or not math.isfinite(self.half_life_bars)
            or self.half_life_bars <= 0
        ):
            raise ValueError("half_life_bars must be a positive finite number")
        refs = tuple(item.strip().upper() for item in self.reference_symbols)
        if any(not item for item in refs) or len(refs) != len(set(refs)):
            raise ValueError("reference_symbols must be non-empty and unique")
        object.__setattr__(self, "reference_symbols", refs)

    @property
    def timeframe(self) -> str:
        return f"{self.bar_minutes}m"

    @property
    def maximum_history(self) -> int:
        """Candles of history needed: exactly the lookback, nothing older."""
        return self.lookback_bars

    def weights(self) -> tuple[float, ...]:
        """Recency weights by lag (index 0 = most recent candle, weight 1)."""
        return tuple(0.5 ** (lag / self.half_life_bars) for lag in range(self.lookback_bars))

    def names(self) -> tuple[str, ...]:
        n = self.lookback_bars
        names: list[str] = []
        for lag in range(n):
            names.extend(f"lag{lag}_{metric}" for metric in _PER_CANDLE)
            if lag <= n - 2:
                names.append(f"lag{lag}_return")
            if lag <= n - 3:
                names.append(f"lag{lag}_acceleration")
        names.extend(f"rw_{metric}" for metric in _SUMMARIZED)
        names.extend(
            (
                "window_return",
                "window_range_pct",
                "window_close_position",
                "window_volatility",
                "close_vs_window_mean",
            )
        )
        if self.include_vwap:
            names.append("vwap_distance")
        names.extend(("time_of_day_sin", "time_of_day_cos"))
        names.extend(f"relative_strength_{ref.lower()}" for ref in self.reference_symbols)
        return tuple(names)


class CandleWindowFeaturePipeline:
    """Stateful point-in-time pipeline over completed aggregated candles.

    State resets at a session change, a missing candle, or any provenance change.  A row is emitted
    only when ``lookback_bars`` contiguous candles of one session (and every declared reference
    series) are available.
    """

    def __init__(self, config: CandleWindowFeatureConfig) -> None:
        self.config = config
        self._schema = FeatureSchema(
            config.feature_id,
            config.version,
            config.names(),
            config.reference_symbols,
            config.timeframe,
        )
        self._weights = config.weights()
        self._history: dict[str, deque[ModelBar]] = defaultdict(
            lambda: deque(maxlen=config.maximum_history)
        )

    @property
    def schema(self) -> FeatureSchema:
        return self._schema

    def reset(self) -> None:
        self._history.clear()

    def _append(self, bar: ModelBar) -> None:
        if bar.timeframe != self.config.timeframe:
            raise ValueError(
                f"feature pipeline expects {self.config.timeframe}; got {bar.timeframe}"
            )
        history = self._history[bar.symbol]
        if history:
            previous = history[-1]
            if bar.start <= previous.start:
                raise ValueError(f"{bar.symbol}: candles must be strictly ascending")
            if (
                previous.session_id != bar.session_id
                or previous.end != bar.start
                or previous.feed != bar.feed
                or previous.adjustment != bar.adjustment
            ):
                history.clear()
        history.append(bar)

    def update(
        self, bar: ModelBar, references: Mapping[str, ModelBar] | None = None
    ) -> FeatureVector | None:
        references = references or {}
        expected = set(self.config.reference_symbols)
        if set(references) - expected:
            raise ValueError("undeclared reference symbol supplied")
        self._append(bar)
        valid_refs = True
        for reference_symbol in self.config.reference_symbols:
            reference = references.get(reference_symbol)
            if reference is None:
                self._history[reference_symbol].clear()
                valid_refs = False
                continue
            if reference.symbol != reference_symbol:
                raise ValueError("reference mapping key does not match candle symbol")
            if reference.start != bar.start or reference.end != bar.end:
                self._history[reference_symbol].clear()
                valid_refs = False
                continue
            if (
                reference.feed != bar.feed
                or reference.adjustment != bar.adjustment
                or reference.timeframe != bar.timeframe
            ):
                raise ValueError("primary and reference observations have mixed provenance")
            self._append(reference)
        if not valid_refs:
            return None
        history = tuple(self._history[bar.symbol])
        if len(history) < self.config.maximum_history:
            return None
        if self.config.include_vwap and bar.vwap is None:
            return None
        reference_history: dict[str, tuple[ModelBar, ...]] = {}
        for reference_symbol in self.config.reference_symbols:
            values = tuple(self._history[reference_symbol])
            if len(values) < self.config.maximum_history:
                return None
            if values[-1].start != bar.start or values[-1].session_id != bar.session_id:
                return None
            reference_history[reference_symbol] = values
        values_ = self._calculate(history, reference_history)
        if tuple(values_) != self.schema.names:
            raise RuntimeError("feature calculation drifted from the declared schema")
        return FeatureVector(
            bar.symbol,
            bar.start,
            bar.end,
            bar.session_id,
            bar.feed,
            self.schema.feature_id,
            self.schema.version,
            values_,
        )

    def transform(
        self,
        bars: Sequence[ModelBar],
        *,
        references: Mapping[str, Sequence[ModelBar]] | None = None,
    ) -> Iterator[FeatureVector]:
        """Batch facade over :meth:`update` (not a second implementation)."""
        self.reset()
        indexes = {
            ref: {bar.start: bar for bar in ref_bars} for ref, ref_bars in (references or {}).items()
        }
        for bar in bars:
            aligned = {ref: index[bar.start] for ref, index in indexes.items() if bar.start in index}
            result = self.update(bar, aligned)
            if result is not None:
                yield result

    # ------------------------------------------------------------------ math

    def _calculate(
        self,
        history: tuple[ModelBar, ...],
        references: Mapping[str, tuple[ModelBar, ...]],
    ) -> dict[str, float]:
        n = self.config.lookback_bars
        candles = list(reversed(history))  # candles[0] is the most recent (lag 0)
        close = [float(item.close) for item in candles]
        volume = [float(item.volume) for item in candles]
        mean_volume = fmean(volume)
        per: dict[str, list[float]] = {metric: [] for metric in _SUMMARIZED}
        for lag, item in enumerate(candles):
            o, h, low, c = float(item.open), float(item.high), float(item.low), close[lag]
            spread = h - low
            per["body_pct"].append((c - o) / o)
            per["upper_wick"].append((h - max(o, c)) / spread if spread > 0 else 0.0)
            per["lower_wick"].append((min(o, c) - low) / spread if spread > 0 else 0.0)
            per["range_pct"].append(spread / c)
            per["relative_volume"].append(volume[lag] / mean_volume if mean_volume > 0 else 0.0)
        per["return"] = [close[lag] / close[lag + 1] - 1.0 for lag in range(n - 1)]
        per["acceleration"] = [per["return"][lag] - per["return"][lag + 1] for lag in range(n - 2)]

        values: dict[str, float] = {}
        for lag in range(n):
            for metric in _PER_CANDLE:
                values[f"lag{lag}_{metric}"] = per[metric][lag]
            if lag <= n - 2:
                values[f"lag{lag}_return"] = per["return"][lag]
            if lag <= n - 3:
                values[f"lag{lag}_acceleration"] = per["acceleration"][lag]
        for metric in _SUMMARIZED:
            series = per[metric]
            weights = self._weights[: len(series)]
            values[f"rw_{metric}"] = sum(w * v for w, v in zip(weights, series, strict=True)) / sum(
                weights
            )

        latest, oldest = candles[0], candles[-1]
        highest = max(float(item.high) for item in candles)
        lowest = min(float(item.low) for item in candles)
        spread = highest - lowest
        values["window_return"] = close[0] / float(oldest.open) - 1.0
        values["window_range_pct"] = spread / close[0]
        values["window_close_position"] = (close[0] - lowest) / spread if spread > 0 else 0.5
        logs = [math.log(close[lag] / close[lag + 1]) for lag in range(n - 1)]
        values["window_volatility"] = pstdev(logs)
        mean_close = fmean(close)
        values["close_vs_window_mean"] = (close[0] - mean_close) / mean_close
        if self.config.include_vwap:
            assert latest.vwap is not None
            vwap = float(latest.vwap)
            values["vwap_distance"] = (close[0] - vwap) / vwap
        local = latest.end.astimezone(_NY)
        minute = local.hour * 60 + local.minute + local.second / 60
        phase = 2 * math.pi * minute / (24 * 60)
        values["time_of_day_sin"] = math.sin(phase)
        values["time_of_day_cos"] = math.cos(phase)
        for reference_symbol, ref_history in references.items():
            ref_candles = list(reversed(ref_history))
            reference_return = float(ref_candles[0].close) / float(ref_candles[-1].open) - 1.0
            values[f"relative_strength_{reference_symbol.lower()}"] = (
                values["window_return"] - reference_return
            )
        return values
