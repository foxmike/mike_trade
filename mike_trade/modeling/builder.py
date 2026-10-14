"""Chronological feature/label alignment without loading the full universe."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime
from typing import Any

from mike_trade.modeling.aggregation import AggregationPolicy, AggregationStats, aggregate_bars
from mike_trade.modeling.contracts import (
    DatasetProvider,
    DatasetSelection,
    FeaturePipeline,
    LabelGenerator,
    ModelBar,
    TrainingExample,
)
from mike_trade.modeling.splits import DEVELOPMENT_START, FINAL_HOLDOUT, HoldoutAccessError
from mike_trade.scheduling.calendar import NEW_YORK, session_for


class ChronologicalTrainingDatasetBuilder:
    """Join point-in-time features to future-only targets on the signal bar key."""

    def __init__(self, feature_pipeline: FeaturePipeline, label_generator: LabelGenerator) -> None:
        self.feature_pipeline = feature_pipeline
        self.label_generator = label_generator

    def build_symbol(
        self,
        bars: Sequence[ModelBar],
        *,
        references: Mapping[str, Sequence[ModelBar]] | None = None,
    ) -> Iterator[TrainingExample]:
        if any(bar.start < DEVELOPMENT_START for bar in bars):
            raise HoldoutAccessError("training data cannot precede 2025-10-09")
        if any(bar.start < FINAL_HOLDOUT.end and bar.end > FINAL_HOLDOUT.start for bar in bars):
            raise HoldoutAccessError(
                "training-data construction cannot inspect the reserved final holdout"
            )
        labels = {
            (label.symbol, label.signal_bar_start): label
            for label in self.label_generator.generate(bars)
        }
        transform = getattr(self.feature_pipeline, "transform", None)
        if callable(transform):
            features = transform(bars, references=references)
        else:
            # Protocol-compatible fallback still uses the streaming implementation.
            reference_indexes = {
                name: {item.start: item for item in values}
                for name, values in (references or {}).items()
            }
            reset = getattr(self.feature_pipeline, "reset", None)
            if callable(reset):
                reset()

            def stream() -> Iterator[object]:
                for bar in bars:
                    aligned = {
                        name: index[bar.start]
                        for name, index in reference_indexes.items()
                        if bar.start in index
                    }
                    value = self.feature_pipeline.update(bar, aligned)
                    if value is not None:
                        yield value

            features = stream()
        for feature in features:
            label = labels.get((feature.symbol, feature.bar_start))
            if label is not None:
                yield TrainingExample(feature, label)

    def iter_selection(
        self,
        provider: DatasetProvider,
        selection: DatasetSelection,
        *,
        symbols: Sequence[str] | None = None,
        batch_size: int = 4096,
    ) -> Iterator[TrainingExample]:
        """Process one primary symbol at a time; never materialize the whole universe."""
        references = self.feature_pipeline.schema.required_reference_symbols
        requested = tuple(
            symbols
            or (source.symbol for source in selection.sources if source.symbol not in references)
        )
        if any(symbol.upper() in references for symbol in requested):
            raise ValueError("a symbol cannot be both the primary series and its own reference")
        reference_bars = {
            ref: tuple(provider.iter_bars(selection, ref, batch_size=batch_size))
            for ref in references
        }
        for symbol in requested:
            normalized = symbol.upper()
            bars = tuple(provider.iter_bars(selection, normalized, batch_size=batch_size))
            yield from self.build_symbol(bars, references=reference_bars)


class AggregatedTrainingDatasetBuilder:
    """Training examples over N-minute candles derived locally from stored one-minute bars.

    The builder receives the symbol's regular-session one-minute bars (and the one-minute reference
    bars), aggregates them with :mod:`mike_trade.modeling.aggregation`, computes features by running
    the candle pipeline's streaming ``update`` over the completed candles, and builds labels from the
    same candles - consulting the one-minute bars only to order two barriers touched inside one
    candle.  Features never see anything newer than their own candle; labels alone look forward.
    """

    def __init__(
        self,
        feature_pipeline: FeaturePipeline,
        label_generator: Any,
        policy: AggregationPolicy,
    ) -> None:
        self.feature_pipeline = feature_pipeline
        self.label_generator = label_generator
        self.policy = policy
        self.aggregation_stats = AggregationStats()
        self._reference_cache: dict[str, tuple[tuple[object, ...], tuple[ModelBar, ...]]] = {}

    def _complete_through(self, bars: Sequence[ModelBar]) -> datetime | None:
        if not bars:
            return None
        day = bars[-1].start.astimezone(NEW_YORK).date()
        return session_for(day).close_utc  # the data covers the whole final session

    def _reference_candles(
        self, name: str, bars: Sequence[ModelBar]
    ) -> tuple[ModelBar, ...]:
        key = (len(bars), bars[0].start if bars else None, bars[-1].start if bars else None)
        cached = self._reference_cache.get(name)
        if cached is not None and cached[0] == key:
            return cached[1]
        candles = aggregate_bars(bars, self.policy, complete_through=self._complete_through(bars))
        self._reference_cache[name] = (key, candles)
        return candles

    def build_symbol(
        self,
        bars: Sequence[ModelBar],
        *,
        references: Mapping[str, Sequence[ModelBar]] | None = None,
    ) -> Iterator[TrainingExample]:
        if any(bar.start < DEVELOPMENT_START for bar in bars):
            raise HoldoutAccessError("training data cannot precede 2025-10-09")
        if any(bar.start < FINAL_HOLDOUT.end and bar.end > FINAL_HOLDOUT.start for bar in bars):
            raise HoldoutAccessError(
                "training-data construction cannot inspect the reserved final holdout"
            )
        candles = aggregate_bars(
            bars,
            self.policy,
            complete_through=self._complete_through(bars),
            stats=self.aggregation_stats,
        )
        minute_index = {bar.start: bar for bar in bars}
        reference_candles = {
            name: self._reference_candles(name, values)
            for name, values in (references or {}).items()
        }
        labels = {
            (label.symbol, label.signal_bar_start): label
            for label in self.label_generator.generate(candles, minute_bars=minute_index)
        }
        for feature in self.feature_pipeline.transform(candles, references=reference_candles):
            label = labels.get((feature.symbol, feature.bar_start))
            if label is not None:
                yield TrainingExample(feature, label)

    def iter_selection(
        self,
        provider: DatasetProvider,
        selection: DatasetSelection,
        *,
        symbols: Sequence[str] | None = None,
        batch_size: int = 4096,
    ) -> Iterator[TrainingExample]:
        references = self.feature_pipeline.schema.required_reference_symbols
        requested = tuple(
            symbols
            or (source.symbol for source in selection.sources if source.symbol not in references)
        )
        reference_bars = {
            ref: tuple(provider.iter_bars(selection, ref, batch_size=batch_size))
            for ref in references
        }
        for symbol in requested:
            bars = tuple(provider.iter_bars(selection, symbol.upper(), batch_size=batch_size))
            yield from self.build_symbol(bars, references=reference_bars)
