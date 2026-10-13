from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from mike_trade.modeling.builder import ChronologicalTrainingDatasetBuilder
from mike_trade.modeling.contracts import (
    FeatureVector,
    LearningTask,
    ModelBar,
    SupervisedLabel,
    TrainingExample,
)
from mike_trade.modeling.features import IntradayFeaturePipeline
from mike_trade.modeling.labels import (
    ExecutionCostAssumptions,
    ForwardReturnConfig,
    ForwardReturnLabelGenerator,
    IntrabarAmbiguityPolicy,
    TripleBarrierConfig,
    TripleBarrierLabelGenerator,
)
from mike_trade.modeling.splits import (
    FINAL_HOLDOUT,
    ExperimentPurpose,
    FinalHoldoutAuthorization,
    FoldPartition,
    HoldoutAccessError,
    PurgedWalkForwardSplitter,
    TimeRange,
    authorize_range,
)

START = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
MINUTE = timedelta(minutes=1)


def _bar(
    minute: int,
    *,
    symbol: str = "AAA",
    open_: str = "100",
    high: str = "100.5",
    low: str = "99.5",
    close: str = "100",
    session: str = "XNYS-2026-03-02",
    start: datetime = START,
) -> ModelBar:
    moment = start + minute * MINUTE
    return ModelBar(
        symbol,
        moment,
        moment + MINUTE,
        D(open_),
        D(high),
        D(low),
        D(close),
        D("1000"),
        "iex",
        "1m",
        "raw",
        session,
        D(close),
    )


def test_forward_return_uses_next_bar_open_and_costs() -> None:
    bars = (
        _bar(0),
        _bar(1, open_="100", high="101", low="99", close="100.5"),
        _bar(2, open_="100.5", high="103", low="100", close="102"),
    )
    generator = ForwardReturnLabelGenerator(
        ForwardReturnConfig(
            2,
            LearningTask.REGRESSION,
            costs=ExecutionCostAssumptions(D("10"), D("10")),
        )
    )
    label = next(generator.generate(bars))
    assert label.entry_time == bars[1].start
    assert label.entry_price == D("100.100")
    assert label.outcome_price == D("101.898")
    assert label.net_return == D("101.898") / D("100.100") - D("1")


def test_missing_next_bar_and_session_boundary_do_not_create_labels() -> None:
    gap = (_bar(0), _bar(2))
    generator = ForwardReturnLabelGenerator(ForwardReturnConfig(1))
    assert list(generator.generate(gap)) == []
    next_session = _bar(
        1,
        start=datetime(2026, 3, 3, 14, 29, tzinfo=UTC),
        session="XNYS-2026-03-03",
    )
    assert list(generator.generate((_bar(0), next_session))) == []


def test_triple_barrier_ambiguous_intrabar_is_loss_or_skipped() -> None:
    bars = (_bar(0), _bar(1, open_="100", high="102", low="98", close="101"))
    loss = TripleBarrierLabelGenerator(TripleBarrierConfig(D("0.01"), D("0.01"), 1))
    label = next(loss.generate(bars))
    assert label.outcome == "LOSS"
    assert label.value == -1
    assert label.outcome_price == D("99")

    skip = TripleBarrierLabelGenerator(
        TripleBarrierConfig(
            D("0.01"),
            D("0.01"),
            1,
            ambiguity_policy=IntrabarAmbiguityPolicy.SKIP,
        )
    )
    assert list(skip.generate(bars)) == []


def test_training_builder_aligns_completed_features_before_future_target() -> None:
    bars = tuple(_bar(minute) for minute in range(25))
    builder = ChronologicalTrainingDatasetBuilder(
        IntradayFeaturePipeline(),
        ForwardReturnLabelGenerator(ForwardReturnConfig(1)),
    )
    examples = tuple(builder.build_symbol(bars))
    assert examples
    assert all(item.features.as_of == item.label.entry_time for item in examples)
    assert all(item.features.bar_start == item.label.signal_bar_start for item in examples)


def _example(symbol: str, feature_time: datetime, outcome_time: datetime) -> TrainingExample:
    bar_start = feature_time - MINUTE
    feature = FeatureVector(
        symbol,
        bar_start,
        feature_time,
        "session",
        "iex",
        "test_features",
        "1.0.0",
        {"value": 1.0},
    )
    label = SupervisedLabel(
        symbol,
        bar_start,
        feature_time,
        D("100"),
        outcome_time,
        D("101"),
        1,
        D("0.01"),
        "test_label",
        "1.0.0",
        "WIN",
    )
    return TrainingExample(feature, label)


def test_purging_is_global_across_symbols_and_applies_embargo() -> None:
    validation_start = datetime(2026, 3, 10, tzinfo=UTC)
    validation = TimeRange(validation_start, validation_start + timedelta(days=1))
    examples = (
        _example(
            "AAA", validation_start - timedelta(days=2), validation_start - timedelta(hours=2)
        ),
        # BBB's feature is old enough, but its overlapping target must be purged globally.
        _example(
            "BBB", validation_start - timedelta(days=2), validation_start + timedelta(hours=1)
        ),
        _example(
            "AAA", validation_start + timedelta(hours=1), validation_start + timedelta(hours=2)
        ),
    )
    fold = next(
        PurgedWalkForwardSplitter((validation,), embargo=timedelta(hours=1)).split(examples)
    )
    assert [item.features.symbol for item in fold.train] == ["AAA"]
    assert len(fold.validation) == 1
    assert max(item.label.outcome_time for item in fold.train) <= validation_start - timedelta(
        hours=1
    )
    assignments = tuple(
        PurgedWalkForwardSplitter((validation,), embargo=timedelta(hours=1)).assign(examples)
    )
    assert [item.partition for item in assignments] == [
        FoldPartition.TRAIN,
        FoldPartition.VALIDATION,
    ]


def test_holdout_is_excluded_from_development_and_requires_locked_authorization() -> None:
    experiment_hash = "a" * 64
    with pytest.raises(HoldoutAccessError, match="exclude holdout"):
        authorize_range(
            FINAL_HOLDOUT, ExperimentPurpose.DEVELOPMENT, experiment_hash=experiment_hash
        )
    with pytest.raises(HoldoutAccessError, match="authorization"):
        authorize_range(
            FINAL_HOLDOUT, ExperimentPurpose.FINAL_HOLDOUT, experiment_hash=experiment_hash
        )
    authorize_range(
        FINAL_HOLDOUT,
        ExperimentPurpose.FINAL_HOLDOUT,
        experiment_hash=experiment_hash,
        authorization=FinalHoldoutAuthorization("operator-token", experiment_hash),
    )
    holdout_bar = _bar(
        0,
        start=FINAL_HOLDOUT.start,
        session="XNYS-2026-09-21",
    )
    builder = ChronologicalTrainingDatasetBuilder(
        IntradayFeaturePipeline(), ForwardReturnLabelGenerator(ForwardReturnConfig(1))
    )
    with pytest.raises(HoldoutAccessError, match="training-data construction"):
        tuple(builder.build_symbol((holdout_bar,)))
