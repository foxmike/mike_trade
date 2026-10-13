"""Chronological feature/label alignment without loading the full universe."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence

from mike_trade.modeling.contracts import (
    DatasetProvider,
    DatasetSelection,
    FeaturePipeline,
    LabelGenerator,
    ModelBar,
    TrainingExample,
)
from mike_trade.modeling.splits import DEVELOPMENT_START, FINAL_HOLDOUT, HoldoutAccessError


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
