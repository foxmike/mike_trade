"""Small deterministic model artifacts and bar helpers for model-strategy tests.

Real native XGBoost / LightGBM boosters are trained in milliseconds on tiny synthetic data and
stored through the production ``FileModelRepository``/manifest path, so the tests exercise the
exact load, verify and predict code the trading runtime uses.  The learned rules are simple and
monotone so decisions are predictable from the input bars:

* BUY (class ``1`` = take-profit first, ``-1`` = stop first, ``0`` = timeout): the sign and size
  of ``return_1`` decides the class.
* SELL (class ``-1`` = risk exit, ``0`` = exit now, ``1`` = hold): the *actual* position
  unrealized return decides the class (loss -> risk exit, large gain -> exit now, else hold).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np

from mike_trade.domain.model_signals import ModelReference
from mike_trade.modeling.adapters.common import LightGBMTrainingConfig, XGBoostTrainingConfig
from mike_trade.modeling.adapters.lightgbm import LightGBMTrainer
from mike_trade.modeling.adapters.xgboost import XGBoostTrainer
from mike_trade.modeling.artifacts import (
    FileModelRepository,
    ModelArtifactManifest,
    schema_fingerprint,
)
from mike_trade.modeling.contracts import (
    DatasetSource,
    FeatureSchema,
    LearningTask,
    ModelBar,
    TrainingRequest,
)
from mike_trade.modeling.features import IntradayFeatureConfig, IntradayFeaturePipeline
from mike_trade.modeling.labels import (
    ExecutionCostAssumptions,
    TripleBarrierConfig,
    TripleBarrierLabelGenerator,
)
from mike_trade.modeling.matrix import MatrixDataset
from mike_trade.modeling.objectives import (
    POSITION_FEATURE_NAMES,
    ScheduledEntryPolicy,
    SellContinuationConfig,
    SellPositionDatasetBuilder,
)
from mike_trade.modeling.splits import TimeRange
from mike_trade.scheduling.calendar import session_for

BUY_FEATURES = ("return_1", "return_5", "vwap_distance", "relative_strength_spy_5")
SELL_MARKET_FEATURES = ("return_1", "vwap_distance")
CLASSES = ("-1", "0", "1")
TRAINED_SYMBOLS = ("AAA", "BBB", "ZZZ", "SPY")
SESSION_OPEN = datetime(2025, 3, 10, 13, 30, tzinfo=UTC)  # 09:30 ET, a regular session
MINUTE = timedelta(minutes=1)


def feature_config() -> IntradayFeatureConfig:
    return IntradayFeatureConfig(reference_symbols=("SPY",))


def buy_schemas() -> tuple[FeatureSchema, object]:
    pipeline = IntradayFeaturePipeline(feature_config())
    labels = TripleBarrierLabelGenerator(
        TripleBarrierConfig(
            Decimal("0.004"),
            Decimal("0.003"),
            30,
            ExecutionCostAssumptions(Decimal("1"), Decimal("1")),
        )
    )
    return pipeline.schema, labels.schema


def sell_builder() -> SellPositionDatasetBuilder:
    return SellPositionDatasetBuilder(
        IntradayFeaturePipeline(feature_config()),
        ScheduledEntryPolicy(warmup_bars=25, maximum_holding_bars=45),
        SellContinuationConfig(
            horizon_bars=10,
            risk_barrier=Decimal("0.008"),
            minimum_hold_advantage=Decimal("0.0005"),
            costs=ExecutionCostAssumptions(Decimal("1"), Decimal("1")),
        ),
    )


def _buy_matrix(rows: int = 900) -> MatrixDataset:
    rng = np.random.default_rng(11)
    r1 = rng.normal(0, 0.003, rows)
    r5 = r1 * 2 + rng.normal(0, 0.001, rows)
    vwap = rng.normal(0, 0.002, rows)
    rel = rng.normal(0, 0.001, rows)
    targets = np.where(r1 > 0.0015, 2, np.where(r1 < -0.0015, 0, 1)).astype(np.float64)
    features = np.column_stack((r1, r5, vwap, rel)).astype(np.float32)
    return MatrixDataset(features, targets, BUY_FEATURES)


def _sell_matrix(rows: int = 1200) -> MatrixDataset:
    rng = np.random.default_rng(13)
    r1 = rng.normal(0, 0.002, rows)
    vwap = rng.normal(0, 0.002, rows)
    unrealized = rng.normal(0.001, 0.006, rows)
    holding = rng.integers(1, 35, rows).astype(np.float64)
    favorable = unrealized + np.abs(rng.normal(0.001, 0.001, rows))
    adverse = unrealized - np.abs(rng.normal(0.001, 0.001, rows))
    # class index 0 = "-1" risk exit, 1 = "0" exit now, 2 = "1" hold
    targets = np.where(unrealized < -0.003, 0, np.where(unrealized > 0.004, 1, 2)).astype(
        np.float64
    )
    features = np.column_stack((r1, vwap, unrealized, holding, favorable, adverse)).astype(
        np.float32
    )
    return MatrixDataset(features, targets, (*SELL_MARKET_FEATURES, *POSITION_FEATURE_NAMES))


@dataclass(frozen=True)
class BuiltModel:
    reference: ModelReference
    root: Path
    manifest: ModelArtifactManifest
    artifact_path: Path


def build_model(
    root: Path,
    *,
    algorithm: str,
    role: str,
    model_id: str | None = None,
    version: str = "1.0.0",
    feed: str = "iex",
    trained_symbols: tuple[str, ...] = TRAINED_SYMBOLS,
) -> BuiltModel:
    """Train, store and return a pinned reference to a tiny real model."""
    model_id = model_id or f"{algorithm}_{role}_test"
    if role == "buy":
        feature_schema, label_schema = buy_schemas()
        matrix = _buy_matrix()
        selected = BUY_FEATURES
        entry_policy: dict[str, str] = {}
    else:
        builder = sell_builder()
        feature_schema, label_schema = builder.feature_schema, builder.label_schema
        matrix = _sell_matrix()
        selected = (*SELL_MARKET_FEATURES, *POSITION_FEATURE_NAMES)
        entry_policy = builder.entry_policy.as_mapping()
    split = int(matrix.row_count * 0.8)
    train = MatrixDataset(matrix.features[:split], matrix.targets[:split], matrix.feature_names)
    validation = MatrixDataset(
        matrix.features[split:], matrix.targets[split:], matrix.feature_names
    )
    task = LearningTask.MULTICLASS_CLASSIFICATION
    request = TrainingRequest(model_id, version, 5, feature_schema, label_schema, {})  # type: ignore[arg-type]
    if algorithm == "xgboost":
        trainer: XGBoostTrainer | LightGBMTrainer = XGBoostTrainer(
            XGBoostTrainingConfig(
                task,
                selected,
                CLASSES,
                threads=1,
                num_boost_round=40,
                early_stopping_rounds=6,
                max_depth=3,
                subsample=1.0,
                colsample_bytree=1.0,
            )
        )
        artifact_format, adapter_id = "xgboost_ubj", "xgboost"
    else:
        trainer = LightGBMTrainer(
            LightGBMTrainingConfig(
                task,
                selected,
                CLASSES,
                threads=1,
                num_boost_round=40,
                early_stopping_rounds=6,
                min_data_in_leaf=5,
                feature_fraction=1.0,
                bagging_fraction=1.0,
            )
        )
        artifact_format, adapter_id = "lightgbm_text", "lightgbm"
    result = trainer.fit(request, train, validation)
    evaluation = b'{"metrics":{"log_loss":0.5}}\n'
    sources = tuple(
        DatasetSource(
            f"{symbol.lower()}-dataset",
            "d" * 64,
            symbol,
            feed,
            "1m",
            "raw",
            datetime(2025, 10, 9, tzinfo=UTC),
            datetime(2026, 9, 21, tzinfo=UTC),
        )
        for symbol in trained_symbols
    )
    manifest = ModelArtifactManifest(
        2,
        model_id,
        version,
        datetime(2026, 8, 31, tzinfo=UTC),
        adapter_id,
        "1.0.0",
        artifact_format,
        hashlib.sha256(result.artifact).hexdigest(),
        len(result.artifact),
        feature_schema,
        schema_fingerprint(feature_schema),
        label_schema,  # type: ignore[arg-type]
        schema_fingerprint(label_schema),  # type: ignore[arg-type]
        sources,
        feed,
        "1m",
        "raw",
        "synthetic_universe",
        (TimeRange(datetime(2025, 10, 9, tzinfo=UTC), datetime(2026, 6, 1, tzinfo=UTC)),),
        (TimeRange(datetime(2026, 6, 2, tzinfo=UTC), datetime(2026, 8, 31, tzinfo=UTC)),),
        "chronological",
        {"entry_bps": "1", "exit_bps": "1"},
        5,
        {"library": "test"},
        "c" * 64,
        {"test_metric": "0.5"},
        False,
        algorithm,
        f"{role}_synthetic_target",
        selected,
        CLASSES,
        {**result.hyperparameters, "best_iteration": result.best_iteration},
        result.calibrator.as_mapping(),
        entry_policy,
        result.feature_importance,
        hashlib.sha256(evaluation).hexdigest(),
        len(evaluation),
    )
    stored = FileModelRepository(root).put(manifest, result.artifact, evaluation)
    return BuiltModel(
        ModelReference(model_id, version, algorithm, role), root, manifest, stored.artifact_path
    )


# ------------------------------------------------------------------------------ bars


def session_id(day: datetime = SESSION_OPEN) -> str:
    return session_for(day.date()).session_id


def model_bars(
    symbol: str,
    closes: list[Decimal],
    *,
    start: datetime = SESSION_OPEN,
    feed: str = "iex",
    vwap_offset: Decimal = Decimal("0"),
    with_vwap: bool = True,
) -> list[ModelBar]:
    """Contiguous regular-session 1-minute bars: open = previous close, ±0.05 wicks."""
    bars: list[ModelBar] = []
    previous = closes[0]
    for index, close in enumerate(closes):
        begin = start + index * MINUTE
        open_price = previous
        high = max(open_price, close) + Decimal("0.05")
        low = min(open_price, close) - Decimal("0.05")
        bars.append(
            ModelBar(
                symbol,
                begin,
                begin + MINUTE,
                open_price,
                high,
                low,
                close,
                Decimal("1000"),
                feed,
                "1m",
                "raw",
                session_id(begin),
                vwap=(close + vwap_offset) if with_vwap else None,
            )
        )
        previous = close
    return bars


def rising(count: int, start: str = "100", step: str = "0.10") -> list[Decimal]:
    return [Decimal(start) + Decimal(step) * i for i in range(count)]


def falling(count: int, start: str = "100", step: str = "0.10") -> list[Decimal]:
    return [Decimal(start) - Decimal(step) * i for i in range(count)]


def flat(count: int, price: str = "100") -> list[Decimal]:
    return [Decimal(price)] * count
