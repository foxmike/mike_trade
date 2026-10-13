# Model training architecture

## Purpose and boundary

`mike_trade.modeling` is the shared, broker-neutral foundation for statistical,
tree-based, and deep-learning models. It is production infrastructure rather than a
model-selection framework. It owns point-in-time data selection, features, targets,
temporal validation, typed training/prediction contracts, and immutable model artifacts.
It does not own broker access, orders, portfolio state, strategy arbitration, or market-data
acquisition.

The initial data scope is the existing history beginning 2025-10-09. The design does not
assume that more years will be acquired. It reads the existing immutable Parquet snapshots
and DuckDB catalog; it does not create another raw market-data store or interfere with SIP
acquisition.

## Components and dependency direction

| Component | Ownership |
| --- | --- |
| `contracts.py` | Frozen domain values and Protocol ports for datasets, features, labels, training, prediction, evaluation, and repositories. |
| `datasets.py` | Read-only adapter over `HistoricalDatasetRepository`, bounded DuckDB scans, and optional immutable derived-feature partitions. |
| `inputs.py` | Strict adapter from the existing completed operational `Candle` to `ModelBar`. |
| `features.py` | The single implementation of the versioned intraday feature schema for batch and streaming use. |
| `labels.py` | Next-bar-aligned forward-return and triple-barrier targets. |
| `builder.py` | Chronological feature/target alignment and one-primary-symbol-at-a-time construction. |
| `splits.py` | Development/holdout guard and globally purged expanding walk-forward folds. |
| `evaluation.py` | Deterministic baseline classification and regression metrics. |
| `registry.py` | Explicit trusted adapter registration, independent of any ML vendor. |
| `artifacts.py` | Strict manifests and atomic, checksum-verified native model artifacts. |

Policy and contracts point inward. Adapters depend on the contracts, the existing research
repository, and pure market/session types. Model trainers must not import a broker or an
operational account. Strategies may eventually depend on a narrow prediction provider that
returns `ModelPrediction`; they must not load artifacts or instantiate XGBoost/LightGBM.
Concrete model-library adapters will be registered at a composition root.

## Public contracts

The package exports these structural interfaces:

- `DatasetProvider.select(...) -> DatasetSelection` and `iter_bars(...)`;
- `FeaturePipeline.schema` and `update(completed_bar, references)`;
- `LabelGenerator.schema` and `generate(chronological_bars)`;
- `TrainingDatasetBuilder.build_symbol(...)`;
- `ModelTrainer.train(TrainingRequest, examples) -> bytes`;
- `ModelPredictor.predict(FeatureVector) -> ModelPrediction`;
- `ModelEvaluator.evaluate(predictions, labels) -> EvaluationReport`;
- `ModelRepository.put/get`, always using an explicit model ID and pinned semantic version.

`ModelPrediction` is deliberately small and broker-neutral. It identifies the model and
feature schema, symbol and completed-bar `as_of`, and carries either a regression value or a
class with optional probabilities. It contains no quantity, order, account, or confidence
shortcut. A strategy remains responsible only for interpreting a prediction as one input to
its normal pure evaluation; existing arbitration, risk, protection, and execution remain in
their current layers.

## Historical dataset selection and lineage

`ParquetDatasetProvider` resolves exactly one immutable snapshot per symbol for a half-open UTC
range. Optional dataset IDs pin the selection. Each source records dataset ID, Parquet SHA-256,
symbol, feed, timeframe, adjustment, and declared coverage. A selection refuses mixed feed,
timeframe, or adjustment identities. The adapter re-verifies each Parquet checksum before it
scans.

DuckDB reads each symbol in configurable batches. `ChronologicalTrainingDatasetBuilder`
materializes only one primary symbol at a time (plus the small declared set of market-reference
series), never all 100 instruments together. The initial adapter emits regular-session bars and
leaves extended-hours observations in their authoritative raw snapshot.

Derived feature partitions are optional performance artifacts. They contain only features and
keys, are versioned/content-addressed, and point back to every raw dataset ID and hash. They do
not copy OHLCV data or replace the raw Parquet/DuckDB facilities. Callers should persist bounded
symbol/time partitions rather than an entire pooled universe in one call.

An experiment/model manifest records:

- every historical dataset ID and SHA-256;
- feed, timeframe, adjustment, and universe ID;
- exact training and validation half-open ranges;
- complete feature schema/version/fingerprint and label schema/version/fingerprint;
- execution assumptions used by targets;
- random seed, adapter identity, software versions, validation method and metrics;
- the frozen training-configuration SHA-256.

IEX and SIP cannot be silently combined within a `DatasetSelection` or model manifest.

## Feature contract

`ModelBar` is a validated completed bar. OHLC must be positive and ordered, volume non-negative,
timestamps aware, and provenance/session identity explicit. `model_bar_from_candle` rejects a
`DEVELOPING` candle, non-RAW price basis, and an unsupported/non-regular session.

`IntradayFeaturePipeline` v1 provides:

- percentage and log returns at 1, 5, and 15 bars;
- rolling realized volatility at 5 and 15 bars;
- 5- and 20-bar volume means and relative volume;
- normalized candle body, upper/lower wick, and current range;
- 20-bar rolling price range and one-bar price acceleration;
- 5- and 20-bar moving-average distance/ratio relationships;
- current-bar VWAP distance when the schema enables VWAP and the value is available;
- 20-bar price z-score and cyclical New York time-of-day fields;
- 5-bar relative strength for every explicitly declared reference symbol.

No row is emitted until the complete declared schema is supported. A missing minute, session
change, feed change, or adjustment change resets that series' rolling state. Reference bars must
match the primary interval and provenance. Numeric outputs must be finite.

There is one calculation path: historical `transform()` resets state and calls the same
streaming `update()` used for inference. Thus identical completed-bar sequences have identical
feature values and ordering. A vector's `as_of` is the bar end, never its start.

Feature and label versions are independent. Changing math, lookback semantics, or field order
requires a new feature version. Changing entry, barrier, horizon, cost, or target semantics
requires a new label version. A model manifest pins both fingerprints as well as versions.

## Label contract and execution alignment

All targets are anchored to a completed signal bar. Hypothetical entry is the open of the next
contiguous, positive-volume bar in the same session and provenance series. This matches the
backtester's `NEXT_BAR_OPEN` convention. If that evidence is missing, no label is produced; an
unavailable historical fill is never assumed. Features end at or before entry. Future bars are
visible only inside the label generator and never enter a `FeatureVector`.

`ForwardReturnLabelGenerator` supports regression, binary classification, and three-way
classification. Its horizon counts bars beginning with the entry bar, exits at the final close,
and applies separately recorded adverse entry/exit basis-point assumptions.

`TripleBarrierLabelGenerator` records `WIN`, `LOSS`, or `TIMEOUT`, outcome time, outcome price,
net return, and the integer target. Positive and negative price barriers and the maximum bar
horizon are configured. A bar touching both barriers is intrinsically unordered. The default
conservative policy records `LOSS`; `SKIP` omits it. A loss gap uses the worse open, while a
favorable gap assumes no improvement beyond the positive barrier. Timeout requires the complete
contiguous horizon.

## Temporal validation and holdout safety

The development period starts at `2025-10-09T00:00:00Z`. The final holdout is exactly the
half-open range `[2026-09-21T00:00:00Z, 2026-10-09T00:00:00Z)`. `authorize_range` refuses any
development, feature-selection, tuning, fitting, or calibration range that overlaps it. Final
evaluation requires the exact range plus an explicit token bound to the already-frozen experiment
hash. A training manifest cannot claim that holdout evaluation occurred.

`PurgedWalkForwardSplitter` creates expanding chronological folds. Its production `assign()` API
emits fold/partition assignments from an already chronological iterator without materializing the
pooled universe; `split()` is a small-study/test convenience that returns tuples. Training examples must have
both their feature time and label outcome resolved before the validation boundary minus the
configured embargo. Purging is global across the pooled sample: the symbol is intentionally not
part of this test, so an overlapping label on stock B is removed at stock A's validation
boundary. Validation labels must resolve inside their validation range. This prevents overlapping
horizons from leaking across either side of a fold.

The holdout authorization only permits final evaluation. It does not authorize retraining,
calibration, threshold changes, feature selection, or hyperparameter selection after results are
seen.

## Artifact format and registration

`ModelArtifactManifest` schema 1 is strict: unknown or missing fields fail decoding. It includes
all lineage listed above, model and adapter semantic versions, native artifact format, byte size,
and SHA-256. Pickle-family formats (`pickle`, `pkl`, `joblib`, `cloudpickle`, `dill`) are rejected.
Future adapters must save a library-native safe format such as XGBoost JSON/UBJSON or LightGBM
text, not arbitrary Python object graphs.

`FileModelRepository` writes artifacts below their content SHA-256 and manifests below
`model_id/model_version`. Files are fsynced and published with a non-replacing hard link; the
manifest is committed last. An identical retry is idempotent. A different artifact or manifest
at an existing pinned version fails instead of replacing it. Reads require the explicit version
and recheck byte size and checksum. There is deliberately no mutable `latest` alias or implicit
promotion. A later promotion process must create an explicit, audited deployment reference and
must never rewrite the active model version.

`ModelAdapterRegistry` accepts only explicit trusted factory objects keyed by adapter ID and
version. There is no dynamic path import, entry-point discovery, or vendor coupling in the
repository. This assignment supplies no XGBoost or LightGBM factory.

## Training and inference lifecycles

Training:

1. Guard the requested range as development.
2. Resolve/pin immutable snapshots and record hashes.
3. Stream one symbol partition at a time through the versioned feature pipeline.
4. Generate future-only targets and join on `(symbol, signal_bar_start)`.
5. Build purged walk-forward folds with an explicit embargo.
6. Fit/tune only on development folds through a registered `ModelTrainer`.
7. Evaluate validation predictions and freeze configuration, metrics, and lineage.
8. Save the adapter's safe native bytes and immutable manifest.
9. In a separately authorized operation only, evaluate the frozen model on final holdout.

Inference:

1. Load an explicitly pinned, checksum-verified model version through its trusted adapter.
2. Convert only a completed operational candle to `ModelBar` and supply declared reference bars.
3. Run the same stateful `IntradayFeaturePipeline.update()` implementation used in training.
4. Confirm feature ID/version/order against the manifest before prediction.
5. Return `ModelPrediction` to a broker-neutral strategy integration adapter.
6. Let existing signal arbitration, risk, stops, and execution handle any resulting signal.

## Assignment 2 integration requirements

XGBoost and LightGBM adapters should implement `ModelTrainer`/`ModelPredictor` and expose trusted
factories through `ModelAdapterRegistry`. They must validate exact feature field order and schema
fingerprint, accept an injected seed, report their library versions, stream or use bounded
matrices, and write native safe formats. They must not add pickle loading, broker dependencies,
implicit `latest` selection, a second dataset store, alternative feature calculations, or access
to the final holdout during training/tuning.
