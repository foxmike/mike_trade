"""Authoritative intraday features for both historical batches and live bar streams."""

from __future__ import annotations

import math
from collections import defaultdict, deque
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from statistics import fmean, pstdev
from zoneinfo import ZoneInfo

from mike_trade.modeling.contracts import FeatureSchema, FeatureVector, ModelBar

_NY = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class IntradayFeatureConfig:
    """Versioned definition of the initial general-purpose intraday feature set."""

    feature_id: str = "intraday_core"
    version: str = "1.0.0"
    return_lookbacks: tuple[int, ...] = (1, 5, 15)
    volatility_windows: tuple[int, ...] = (5, 15)
    volume_windows: tuple[int, ...] = (5, 20)
    moving_average_windows: tuple[int, ...] = (5, 20)
    range_window: int = 20
    zscore_window: int = 20
    relative_strength_lookback: int = 5
    reference_symbols: tuple[str, ...] = ()
    include_vwap: bool = True
    timeframe: str = "1m"

    def __post_init__(self) -> None:
        for name in (
            "return_lookbacks",
            "volatility_windows",
            "volume_windows",
            "moving_average_windows",
        ):
            values = getattr(self, name)
            if not values or any(
                isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in values
            ):
                raise ValueError(f"{name} must contain positive ints")
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"{name} must be sorted and unique")
        for name in ("range_window", "zscore_window", "relative_strength_lookback"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 2:
                raise ValueError(f"{name} must be an int >= 2")
        refs = tuple(item.strip().upper() for item in self.reference_symbols)
        if any(not item for item in refs) or len(refs) != len(set(refs)):
            raise ValueError("reference_symbols must be non-empty and unique")
        object.__setattr__(self, "reference_symbols", refs)

    @property
    def maximum_history(self) -> int:
        return max(
            max(self.return_lookbacks) + 1,
            max(self.volatility_windows) + 1,
            max(self.volume_windows),
            max(self.moving_average_windows),
            self.range_window,
            self.zscore_window,
            self.relative_strength_lookback + 1,
            3,
        )

    def names(self) -> tuple[str, ...]:
        names: list[str] = []
        for lookback in self.return_lookbacks:
            names.extend((f"return_{lookback}", f"log_return_{lookback}"))
        names.extend(f"realized_vol_{window}" for window in self.volatility_windows)
        for window in self.volume_windows:
            names.extend((f"volume_mean_{window}", f"relative_volume_{window}"))
        names.extend(
            (
                "candle_body_pct",
                "upper_wick_pct",
                "lower_wick_pct",
                "bar_range_pct",
                f"rolling_range_{self.range_window}",
                "price_acceleration",
            )
        )
        for window in self.moving_average_windows:
            names.extend((f"ma_distance_{window}", f"close_to_ma_{window}"))
        if len(self.moving_average_windows) >= 2:
            names.append(
                f"ma_ratio_{self.moving_average_windows[0]}_{self.moving_average_windows[-1]}"
            )
        if self.include_vwap:
            names.append("vwap_distance")
        names.extend(
            (
                f"rolling_zscore_{self.zscore_window}",
                "time_of_day_sin",
                "time_of_day_cos",
            )
        )
        names.extend(
            f"relative_strength_{ref.lower()}_{self.relative_strength_lookback}"
            for ref in self.reference_symbols
        )
        return tuple(names)


class IntradayFeaturePipeline:
    """Stateful point-in-time pipeline with identical batch and streaming math.

    State resets at a session change or any missing bar. A row is emitted only after
    every configured lookback, VWAP input, and declared reference series is available.
    """

    def __init__(self, config: IntradayFeatureConfig | None = None) -> None:
        self.config = config or IntradayFeatureConfig()
        self._schema = FeatureSchema(
            self.config.feature_id,
            self.config.version,
            self.config.names(),
            self.config.reference_symbols,
            self.config.timeframe,
        )
        self._history: dict[str, deque[ModelBar]] = defaultdict(
            lambda: deque(maxlen=self.config.maximum_history)
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
                raise ValueError(f"{bar.symbol}: bars must be strictly ascending")
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
        """Consume one completed primary bar and same-interval reference bars."""
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
                raise ValueError("reference mapping key does not match bar symbol")
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
            reference_values = tuple(self._history[reference_symbol])
            if len(reference_values) < self.config.relative_strength_lookback + 1:
                return None
            if (
                reference_values[-1].start != bar.start
                or reference_values[-1].session_id != bar.session_id
            ):
                return None
            reference_history[reference_symbol] = reference_values
        values = self._calculate(history, reference_history)
        if tuple(values) != self.schema.names:
            raise RuntimeError("feature calculation drifted from the declared schema")
        return FeatureVector(
            bar.symbol,
            bar.start,
            bar.end,
            bar.session_id,
            bar.feed,
            self.schema.feature_id,
            self.schema.version,
            values,
        )

    def transform(
        self,
        bars: Sequence[ModelBar],
        *,
        references: Mapping[str, Sequence[ModelBar]] | None = None,
    ) -> Iterator[FeatureVector]:
        """Batch facade over :meth:`update`; this is intentionally not a second implementation."""
        self.reset()
        reference_indexes = {
            ref: {bar.start: bar for bar in ref_bars}
            for ref, ref_bars in (references or {}).items()
        }
        for bar in bars:
            aligned = {
                ref: index[bar.start]
                for ref, index in reference_indexes.items()
                if bar.start in index
            }
            result = self.update(bar, aligned)
            if result is not None:
                yield result

    def _calculate(
        self,
        history: tuple[ModelBar, ...],
        references: Mapping[str, tuple[ModelBar, ...]],
    ) -> dict[str, float]:
        cfg = self.config
        close = [float(item.close) for item in history]
        volume = [float(item.volume) for item in history]
        latest = history[-1]
        current = close[-1]
        values: dict[str, float] = {}

        for lookback in cfg.return_lookbacks:
            ratio = current / close[-1 - lookback]
            values[f"return_{lookback}"] = ratio - 1.0
            values[f"log_return_{lookback}"] = math.log(ratio)

        one_bar_logs = [
            math.log(right / left) for left, right in zip(close, close[1:], strict=False)
        ]
        for window in cfg.volatility_windows:
            values[f"realized_vol_{window}"] = pstdev(one_bar_logs[-window:])
        for window in cfg.volume_windows:
            mean_volume = fmean(volume[-window:])
            values[f"volume_mean_{window}"] = mean_volume
            values[f"relative_volume_{window}"] = (
                volume[-1] / mean_volume if mean_volume > 0 else 0.0
            )

        open_price, high, low = float(latest.open), float(latest.high), float(latest.low)
        spread = high - low
        values["candle_body_pct"] = (current - open_price) / open_price
        if spread > 0:
            values["upper_wick_pct"] = (high - max(open_price, current)) / spread
            values["lower_wick_pct"] = (min(open_price, current) - low) / spread
        else:
            values["upper_wick_pct"] = 0.0
            values["lower_wick_pct"] = 0.0
        values["bar_range_pct"] = spread / current
        range_bars = history[-cfg.range_window :]
        values[f"rolling_range_{cfg.range_window}"] = (
            float(max(item.high for item in range_bars) - min(item.low for item in range_bars))
            / current
        )
        latest_return = current / close[-2] - 1.0
        previous_return = close[-2] / close[-3] - 1.0
        values["price_acceleration"] = latest_return - previous_return

        moving_averages: dict[int, float] = {}
        for window in cfg.moving_average_windows:
            average = fmean(close[-window:])
            moving_averages[window] = average
            values[f"ma_distance_{window}"] = (current - average) / average
            values[f"close_to_ma_{window}"] = current / average
        if len(cfg.moving_average_windows) >= 2:
            short, long = cfg.moving_average_windows[0], cfg.moving_average_windows[-1]
            values[f"ma_ratio_{short}_{long}"] = moving_averages[short] / moving_averages[long]
        if cfg.include_vwap:
            assert latest.vwap is not None
            vwap = float(latest.vwap)
            values["vwap_distance"] = (current - vwap) / vwap

        z_values = close[-cfg.zscore_window :]
        z_mean = fmean(z_values)
        z_std = pstdev(z_values)
        values[f"rolling_zscore_{cfg.zscore_window}"] = (
            (current - z_mean) / z_std if z_std > 0 else 0.0
        )
        local = latest.end.astimezone(_NY)
        minute = local.hour * 60 + local.minute + local.second / 60
        phase = 2 * math.pi * minute / (24 * 60)
        values["time_of_day_sin"] = math.sin(phase)
        values["time_of_day_cos"] = math.cos(phase)

        lookback = cfg.relative_strength_lookback
        primary_return = current / close[-1 - lookback] - 1.0
        for reference_symbol, ref_history in references.items():
            reference_return = (
                float(ref_history[-1].close) / float(ref_history[-1 - lookback].close) - 1.0
            )
            values[f"relative_strength_{reference_symbol.lower()}_{lookback}"] = (
                primary_return - reference_return
            )
        return values
